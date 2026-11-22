"""Information extraction client orchestrating PII masking, LLM calls, and embeddings."""

import io
import json
import re
import time
from typing import Any, Dict

from pydantic import BaseModel, ValidationError
from kbfabric_ingest.ingest.models import BlockCategory, CanonicalBlock, EnrichedBlock, get_block_category_to_node_mapping
from kbfabric_ingest.ingest.parser import generate_block_id
from kbfabric_ingest.ingest.pii import PIIService
from kbfabric_ingest.ingest.config import get_ontology, get_app_config
from kbfabric_ingest.ingest.ontology_admission import compile_relationship_triples as _compile_relationship_triples
from kbfabric_ingest.llm.client import LLMClient
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import safe_len
from kbfabric_ingest.ingest.vision_utils import (
    ImagePreprocessor,
    should_process_image,
    detect_image_cutoff,
)
from kbfabric_ingest.ingest.ontology_prompt import build_full_ontology_prompt
from kbfabric_ingest.ingest.ontology_admission import OntologyAdmissionEngine
from kbfabric_ingest.ingest.requirement_entity_fields import (
    description_from_kv_pairs as _description_from_kv_pairs,
    extract_entity_description_from_text as _extract_entity_description_from_text,
    entity_with_optional_fields as _entity_with_optional_fields,
    extract_requirement_description_from_text as _extract_requirement_description_from_text,
    parse_kv_pairs as _parse_kv_pairs,
)

logger = get_logger(__name__)

INC_RE = re.compile(
    r"\bINC-[A-Z0-9]+-\d+\b"                                   # INC-LOAN-089, INC-PAY-2024-041, INC-2024-001
    r"|\bINC_\d+\b",                                            # INC_001
    re.IGNORECASE,
)
DEF_RE = re.compile(
    r"\bDEF-[A-Z0-9]+-\d+\b"                                   # DEF-2024-0567, DEF-CL-001
    r"|\b[Ss][Yy][Nn]-[A-Za-z]+-\d+\b"                         # SYN-CL-3015
    r"|\bBUG-\d+\b"                                             # BUG-12345
    r"|\bDEF_\d+\b",                                            # DEF_001
    re.IGNORECASE,
)
TC_RE = re.compile(
    r"\b[Tt][Cc]-[A-Za-z]+-\d+\b"                          # TC-LOAN-001, tc-loan-001
    r"|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]{2,})*_TC_\d+\b"       # CLOS_DEL_SCN_004_TC_001
    r"|\bTC_\d+\b"                                          # TC_001 (bare)
)
SCENARIO_RE = re.compile(r"\b(?:E2E-\d+|E2E\d+|Scenario-[A-Z0-9-]+)\b", re.IGNORECASE)
USER_STORY_RE = re.compile(r"^(?:as an?\b|as the\b|in order to\b)", re.IGNORECASE)
REQ_RE = re.compile(
    r"\b[Rr][Ee][Qq]-[A-Za-z]+-\d+\b"                      # REQ-LOAN-001
    r"|\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]{2,})*_RTM_\d+\b"      # DEL_CL_RTM_001
    r"|\b[Ff][Rr]-\d+\b"                                    # FR-04
)
AC_RE = re.compile(r"\bAC-[A-Z0-9-]+\b", re.IGNORECASE)
BUSINESS_RULE_RE = re.compile(r"\b(?:must|shall|should|required|if|when|then|whereas)\b", re.IGNORECASE)
PROCESS_LINE_RE = re.compile(r"^Related Process:\s*(.+)$", re.IGNORECASE)
TESTCASE_LINE_RE = re.compile(r"^Related Test Case:\s*(.+)$", re.IGNORECASE)

_TC_HEADER_PATTERNS = re.compile(
    r"test[\s_-]?case[\s_-]?id|tc[\s_-]?id|testcase[\s_-]?id",
    re.IGNORECASE,
)
_REQ_HEADER_PATTERNS = re.compile(
    r"requirement[\s_-]?id|req[\s_-]?id|rtm[\s_-]?id",
    re.IGNORECASE,
)
_SCENARIO_HEADER_PATTERNS = re.compile(
    r"scenario[\s_-]?id|scenario[\s_-]?name",
    re.IGNORECASE,
)
_DEF_HEADER_PATTERNS = re.compile(
    r"defect[\s_-]?id|bug[\s_-]?id|issue[\s_-]?id|defect[\s_-]?no",
    re.IGNORECASE,
)
_INC_HEADER_PATTERNS = re.compile(
    r"incident[\s_-]?id|inc[\s_-]?id|incident[\s_-]?no",
    re.IGNORECASE,
)
_USERSTORY_HEADER_PATTERNS = re.compile(
    r"user[\s_-]?story[\s_-]?id|story[\s_-]?id|us[\s_-]?id|jira[\s_-]?id",
    re.IGNORECASE,
)


class EntityStandardizer:
    """Standardizes entity names for consistency in knowledge graph.

    Configurable standardization rules loaded from YAML configuration.
    Supports domain-specific and type-specific standardization mappings.
    Define mappings in enterprise_banking_ontology.yaml under
    domains.<domain>.entity_standardization — no hardcoded defaults.
    """

    DEFAULT_MAPPINGS: Dict[str, Dict[str, str]] = {}

    def __init__(self, domain: str = "default"):
        """Initialize standardizer with domain-specific configuration."""
        self.domain = domain
        self.mappings = self._load_mappings()
        logger.info("EntityStandardizer initialized", domain=domain, mappings_loaded=len(self.mappings))

    def _load_mappings(self) -> Dict[str, Dict[str, str]]:
        """Load standardization mappings from configuration."""
        try:
            from kbfabric_ingest.ingest.config import get_app_config

            config = get_app_config()
            domain_config = config.get("domains", {}).get(self.domain, {})

            # Start with defaults
            mappings = self.DEFAULT_MAPPINGS.copy()

            # Override with domain-specific mappings if available
            entity_standardization = domain_config.get("entity_standardization", {})
            for entity_type, type_mappings in entity_standardization.items():
                if entity_type not in mappings:
                    mappings[entity_type] = {}
                mappings[entity_type].update(type_mappings)

            return mappings

        except Exception as e:
            logger.warning("Failed to load entity standardization config, using defaults", error=str(e))
            return self.DEFAULT_MAPPINGS.copy()

    def standardize_entity(self, entity_type: str, entity_value: str) -> str:
        """Standardize an entity value based on its type and domain."""
        if not entity_value or not isinstance(entity_value, str):
            return entity_value

        normalized_value = entity_value.strip().lower()

        # Look up in type-specific mappings
        type_mappings = self.mappings.get(entity_type, {})
        if normalized_value in type_mappings:
            return type_mappings[normalized_value]

        # Default: title case for unknown values
        return entity_value.title()


class IEResult(BaseModel):
    """Pydantic model for validating LLM IE results."""

    block_category: str
    kg_node_type: str | None = None
    spine_node: str = "domain_root"
    entities: list[dict[str, Any]]
    relationships: list[dict[str, Any]]


class ImageIEResult(BaseModel):
    """Pydantic model for validating LLM vision IE results."""

    description: str
    block_category: str
    kg_node_type: str | None = None
    spine_node: str = "domain_root"
    entities: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    extracted_fields: list[dict[str, Any]] | None = None
    extracted_knowledge: list[dict[str, Any]] | None = None
    semantic_chunks: list[dict[str, Any]] | None = None
    api_method: str = "unknown"  # "chat" or "generate" - tracks KV cache usage
    vision_model_used: str | None = None  # Model that succeeded (primary or fallback)


def _normalize_entity_key(entity: dict[str, Any]) -> tuple[str, str]:
    """Return (type, normalized_value) for entity dedup."""
    t = (entity.get("type") or "Entity").strip()
    v = (entity.get("value") or "").strip().lower()
    return (t, v)


def _merge_tiled_image_results(
    blocks: list[CanonicalBlock],
    results: list[ImageIEResult],
) -> tuple[CanonicalBlock, ImageIEResult]:
    """Merge multiple strip IE results into one block and one result (one chunk per image)."""
    if not blocks or not results or len(blocks) != len(results):
        raise ValueError("blocks and results must be non-empty and same length")
    first_block = blocks[0]
    first_result = results[0]
    if len(blocks) == 1:
        return first_block, first_result

    # Unified description: all strip descriptions in order
    descriptions = [r.description.strip() for r in results if r.description.strip()]
    merged_description = "\n\n".join(descriptions) if descriptions else first_result.description

    # Entities: dedupe by (type, normalized value); keep first occurrence
    seen: set[tuple[str, str]] = set()
    merged_entities: list[dict[str, Any]] = []
    for r in results:
        for e in r.entities or []:
            key = _normalize_entity_key(e)
            if key not in seen:
                seen.add(key)
                merged_entities.append(e)

    # Canonical name map: normalized value -> first display value (for relationship resolution)
    name_to_canonical: dict[str, str] = {}
    for e in merged_entities:
        v = (e.get("value") or "").strip()
        if not v:
            continue
        norm = v.strip().lower()
        if norm not in name_to_canonical:
            name_to_canonical[norm] = v

    # Relationships: resolve from/to to canonical; dedupe (from, type, to)
    rel_seen: set[tuple[str, str, str]] = set()
    merged_rels: list[dict[str, Any]] = []
    for r in results:
        for rel in r.relationships or []:
            from_v = (rel.get("from_value") or "").strip()
            to_v = (rel.get("to_value") or "").strip()
            from_c = name_to_canonical.get(from_v.lower(), from_v) if from_v else ""
            to_c = name_to_canonical.get(to_v.lower(), to_v) if to_v else ""
            key = (from_c, (rel.get("relationship") or rel.get("type") or "").strip(), to_c)
            if key not in rel_seen and from_c and to_c:
                rel_seen.add(key)
                merged_rels.append({
                    **rel,
                    "from_value": from_c,
                    "to_value": to_c,
                })

    # Merge extracted_fields, extracted_knowledge, semantic_chunks (concat)
    all_fields: list[dict[str, Any]] = []
    all_knowledge: list[dict[str, Any]] = []
    all_chunks: list[dict[str, Any]] = []
    for r in results:
        if getattr(r, "extracted_fields", None):
            all_fields.extend(r.extracted_fields)
        if getattr(r, "extracted_knowledge", None):
            all_knowledge.extend(r.extracted_knowledge)
        if getattr(r, "semantic_chunks", None):
            all_chunks.extend(r.semantic_chunks)

    merged_result = ImageIEResult(
        description=merged_description,
        block_category=first_result.block_category,
        kg_node_type=first_result.kg_node_type,
        spine_node=getattr(first_result, 'spine_node', None) or "domain_root",
        entities=merged_entities,
        relationships=merged_rels,
        extracted_fields=all_fields or None,
        extracted_knowledge=all_knowledge or None,
        semantic_chunks=all_chunks or None,
        api_method=first_result.api_method,
        vision_model_used=first_result.vision_model_used,
    )

    # Block: use first strip block; source_block_ids = all strip block_ids for provenance
    all_block_ids = [b.block_id for b in blocks]
    block_dict = first_block.model_dump()
    block_dict["source_block_ids"] = all_block_ids
    merged_block = CanonicalBlock(**block_dict)
    return merged_block, merged_result


def _build_tiled_output_units(image_blocks: list[CanonicalBlock]) -> list[list[int]]:
    """
    Group image blocks by (source_file, page_number). For groups where all blocks
    share the same tile_strip_total and we have strips 0..total-1, return one unit
    (list of indices); otherwise each block is its own unit.
    """
    from collections import defaultdict
    # key -> list of (index, strip_index, strip_total)
    groups: dict[tuple[str, int], list[tuple[int, int | None, int | None]]] = defaultdict(list)
    for i, blk in enumerate(image_blocks):
        meta = getattr(blk, "metadata", None) or {}
        page = getattr(blk, "page_number", None)
        page = int(page) if page is not None else 0
        key = (blk.source_file or "", page)
        strip_idx = meta.get("tile_strip_index")
        strip_total = meta.get("tile_strip_total")
        if strip_total is not None and strip_total < 1:
            strip_total = None
        groups[key].append((i, strip_idx, strip_total))

    units: list[list[int]] = []
    for _key, items in groups.items():
        # Check if this group is a full tiled set (strips 0..total-1)
        strip_totals = {t for (_, _, t) in items if t is not None}
        if len(strip_totals) != 1:
            for (idx, _, _) in items:
                units.append([idx])
            continue
        total = next(iter(strip_totals))
        if total <= 1:
            for (idx, _, _) in items:
                units.append([idx])
            continue
        # Full set: indices 0..total-1 present?
        by_strip = {s: idx for (idx, s, t) in items if t == total and s is not None}
        if len(by_strip) == total and set(by_strip.keys()) == set(range(total)):
            ordered = [by_strip[s] for s in range(total)]
            units.append(ordered)
        else:
            for (idx, _, _) in items:
                units.append([idx])
    return units


from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext

class IEClient:
    """Client for orchestrating information extraction."""

    def __init__(
        self,
        pii_service: PIIService,
        llm_client: LLMClient
    ):
        """
        Initialize IE client.
        
        Args:
            pii_service: PII masking service
            llm_client: LLM client for text and vision IE
        """
        self.pii_service = pii_service
        self.llm_client = llm_client
        self.logger = get_logger(__name__)
        self.ontology_admission: OntologyAdmissionEngine | None = None
        self._alias_cache: dict[tuple[str, str], list[str]] = {}
        self._failure_causes: dict[str, int] = {}  # IE failure breakdown for report
        # Shared reusable pipeline deps (classify / extract / map utilities)
        self._pipeline_deps: Any | None = None
        self._runtime_ontology: dict[str, Any] | None = None
        self._run_context: IngestionRunContext | None = None

    def configure_run_context(self, run_context: IngestionRunContext) -> None:
        """Configure the immutable authoritative context for one ingestion run."""
        if not isinstance(run_context, IngestionRunContext):
            raise TypeError("run_context must be an IngestionRunContext")

        from kbfabric_ingest.ingest.lane2_two_pass import build_lane2_ontology

        runtime_ontology = run_context.ontology
        composed_ontology = build_lane2_ontology(
            customer_ontology=runtime_ontology.domain_definition,
            canonical_model=runtime_ontology.canonical_model,
            extraction_policy=runtime_ontology.extraction_policy,
        )

        composed_ontology["frozen_run_scope"] = {
            "industry_id": runtime_ontology.ontology_id,
            "customer_id": run_context.tenant_context.customer_id,
            "tenant_id": run_context.tenant_context.tenant_id,
            "domain_id": runtime_ontology.domain_id,
            "product_line_id": runtime_ontology.product_line_id,
            "product_line_name": runtime_ontology.product_line_name,
            "customer_ontology_id": str(runtime_ontology.customer_ontology_id),
            "customer_ontology_version": runtime_ontology.customer_ontology_version,
        }
        self._run_context = run_context
        self._runtime_ontology = composed_ontology
        self._pipeline_deps = None
        self.ontology_admission = OntologyAdmissionEngine(composed_ontology)

        self.logger.info(
            "ie.run_context.configured",
            tenant_id=run_context.tenant_context.tenant_id,
            customer_id=run_context.tenant_context.customer_id,
            domain_id=run_context.ontology.domain_id,
            product_line_id=run_context.ontology.product_line_id,
            customer_ontology_id=str(run_context.ontology.customer_ontology_id),
            customer_ontology_version=run_context.ontology.customer_ontology_version,
        )

    def _get_pipeline_deps(self) -> Any:
        """Lazy ``PipelineDeps`` for reusable classify/extract/map utilities."""
        if self._pipeline_deps is not None:
            return self._pipeline_deps
        from kbfabric_ingest.ingest.pipeline import PipelineDeps

        if self._run_context is None:
            raise RuntimeError("IEClient run context has not been configured")

        runtime_ontology = self._run_context.ontology
        ontology = self._runtime_ontology
        if ontology is None:
            raise RuntimeError("IEClient runtime ontology has not been composed")

        runtime_domain = next(
            (
                item for item in (ontology.get("domains", []) or [])
                if isinstance(item, dict) and item.get("name") == runtime_ontology.domain_id
            ),
            {},
        )
        self._pipeline_deps = PipelineDeps.from_clients(
            llm_client=self.llm_client,
            ontology=ontology,
            run_scope={
                "customer_id": self._run_context.tenant_context.customer_id,
                "tenant_id": self._run_context.tenant_context.tenant_id,
                "domain_id": runtime_ontology.domain_id,
                "domain_display_name": runtime_domain.get("display_name") or runtime_ontology.domain_id,
                "product_line_id": runtime_ontology.product_line_id,
                "product_line_name": runtime_ontology.product_line_name,
                "customer_ontology_version": runtime_ontology.customer_ontology_version,
                "industry_id": runtime_ontology.ontology_id,
            },
        )
        self.logger.info(
            "pipeline.deps_ready",
            mapping_enabled=self._pipeline_deps.mapping_enabled,
            llm_adjudication=self._pipeline_deps.use_llm_adjudication,
            neo4j_candidates=self._pipeline_deps.candidate_reader is not None,
            has_llm_callable=self._pipeline_deps.llm_callable is not None,
        )
        return self._pipeline_deps

    def _apply_entity_mapping(
        self,
        *,
        lane: str,
        block: CanonicalBlock,
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        source_context: str = "",
    ) -> list[dict[str, Any]]:
        """Annotate entities via reusable ``step_map_entities`` utility (all lanes)."""
        if not entities:
            return entities
        from kbfabric_ingest.ingest.pipeline import step_map_entities

        deps = self._get_pipeline_deps()
        annotated, results = step_map_entities(
            entities,
            relationships,
            block=block,
            lane=lane,
            deps=deps,
            source_context=source_context,
        )
        self.logger.info(
            "entity_mapping.block_complete",
            lane=lane,
            block_id=block.block_id,
            entity_count=len(annotated),
            reused_count=sum(1 for r in results if r.selected_entity_id),
            create_new_count=sum(1 for r in results if r.decision.value == "CREATE_NEW"),
            requires_review_count=sum(
                1 for r in results if r.decision.value == "REQUIRES_REVIEW"
            ),
            neo4j_candidates_available=deps.candidate_reader is not None,
            llm_adjudication=deps.use_llm_adjudication,
        )
        return annotated

    def _rule_based_ie(self, block: CanonicalBlock) -> dict[str, Any] | None:
        text = (block.text or "").strip()
        if not text:
            return None

        # Structured table rows — Key=Value extraction takes priority over
        # flat-text regex for table_row/test_case_row because it understands
        # column context (primary vs reference columns).
        if block.block_type in ("table_row", "test_case_row"):
            result = self._extract_structured_table_row(block, text)
            if result is not None:
                return result

        # Glossary rows → Reference category (semantic only, no KG)
        if block.block_type == "glossary_row":
            return {
                "block_category": "Reference",
                "kg_node_type": None,
                "entities": [],
                "relationships": [],
            }

        # Incident blocks (narrative text with INC-xxx, or explicit incident_row)
        if INC_RE.search(text) or block.block_type == "incident_row":
            inc_id = INC_RE.search(text)
            inc_value = inc_id.group(0) if inc_id else text.splitlines()[0][:80]
            entities = [{"type": "Incident", "value": inc_value, "confidence": 0.99}]
            relationships = []
            for tc in TC_RE.findall(text):
                entities.append({"type": "TestCase", "value": tc, "confidence": 0.95})
                logger.info(
                    "Incident extraction retained TestCase entity without invalid "
                    "IMPACTS edge: incident=%s test_case=%s",
                    inc_value,
                    tc,
                )
            for df in DEF_RE.findall(text):
                entities.append({"type": "Defect", "value": df, "confidence": 0.95})
                relationships.append({
                    "from_type": "Defect", "from_value": df,
                    "rel_type": "LINKED_TO", "to_type": "Incident", "to_value": inc_value,
                    "confidence": 0.95,
                })
            for line in text.splitlines():
                m = PROCESS_LINE_RE.match(line.strip())
                if m:
                    for p in [x.strip() for x in m.group(1).split(",") if x.strip()]:
                        relationships.append({
                            "from_type": "Incident", "from_value": inc_value,
                            "rel_type": "IMPACTS", "to_type": "Process", "to_value": p,
                            "confidence": 0.95,
                        })
                        entities.append({"type": "Process", "value": p, "confidence": 0.95})
            return {
                "block_category": "Incident",
                "kg_node_type": "Incident",
                "entities": entities,
                "relationships": relationships,
            }

        # Defect blocks (narrative text with DEF-xxx/SYN-xxx/BUG-xxx, or explicit defect_row)
        if DEF_RE.search(text) or block.block_type == "defect_row":
            def_id = DEF_RE.search(text)
            def_value = def_id.group(0) if def_id else text.splitlines()[0][:80]
            entities = [{"type": "Defect", "value": def_value, "confidence": 0.99}]
            relationships = []
            for tc in TC_RE.findall(text):
                entities.append({"type": "TestCase", "value": tc, "confidence": 0.95})
                relationships.append({
                    "from_type": "Defect", "from_value": def_value,
                    "rel_type": "MISSED_BY", "to_type": "TestCase", "to_value": tc,
                    "confidence": 0.90,
                })
            for inc in INC_RE.findall(text):
                entities.append({"type": "Incident", "value": inc, "confidence": 0.95})
                relationships.append({
                    "from_type": "Defect", "from_value": def_value,
                    "rel_type": "LINKED_TO", "to_type": "Incident", "to_value": inc,
                    "confidence": 0.90,
                })
            for req in REQ_RE.findall(text):
                entities.append({"type": "Requirement", "value": req, "confidence": 0.90})
                relationships.append({
                    "from_type": "Defect", "from_value": def_value,
                    "rel_type": "VIOLATES", "to_type": "Requirement", "to_value": req,
                    "confidence": 0.85,
                })
            for line in text.splitlines():
                m = PROCESS_LINE_RE.match(line.strip())
                if m:
                    for p in [x.strip() for x in m.group(1).split(",") if x.strip()]:
                        relationships.append({
                            "from_type": "Defect", "from_value": def_value,
                            "rel_type": "IMPACTS", "to_type": "Process", "to_value": p,
                            "confidence": 0.95,
                        })
                        entities.append({"type": "Process", "value": p, "confidence": 0.95})
            return {
                "block_category": "Defect",
                "kg_node_type": "Defect",
                "entities": entities,
                "relationships": relationships,
            }

        # Test case rows — legacy regex fallback for non-Key=Value blocks
        if block.block_type == "test_case_row":
            tcs = TC_RE.findall(text)
            if tcs:
                tc = tcs[0]
                entities = [{"type": "TestCase", "value": tc, "confidence": 0.99}]
                relationships = []
                for req in REQ_RE.findall(text):
                    entities.append({"type": "Requirement", "value": req, "confidence": 0.90})
                    relationships.append({
                        "from_type": "TestCase", "from_value": tc,
                        "rel_type": "COVERS", "to_type": "Requirement", "to_value": req,
                        "confidence": 0.90
                    })
                return {
                    "block_category": "TestCase",
                    "kg_node_type": "TestCase",
                    "entities": entities,
                    "relationships": relationships
                }

        first_line = ""
        if text:
            for line in text.splitlines():
                candidate = line.strip()
                if candidate:
                    first_line = candidate
                    break
        tc_in_first_line = bool(TC_RE.search(first_line))
        starts_with_test_case = first_line.lower().startswith("test case")
        if first_line and (tc_in_first_line or starts_with_test_case):
            tcs = TC_RE.findall(text)
            if tcs:
                tc = tcs[0]
            else:
                return None
            return {
                "block_category": "TestCase",
                "kg_node_type": "TestCase",
                "entities": [{"type": "TestCase", "value": tc, "confidence": 0.99}],
                "relationships": []
            }

        if SCENARIO_RE.search(text):
            scenario = SCENARIO_RE.search(text)
            scenario_value = scenario.group(0) if scenario else text.splitlines()[0][:80]
            return {
                "block_category": "TestCase",
                "kg_node_type": "TestCase",
                "entities": [{"type": "TestCase", "value": scenario_value, "confidence": 0.95}],
                "relationships": []
            }

        if USER_STORY_RE.match(text) or text.lower().startswith("as a "):
            return {
                "block_category": "UserStory",
                "kg_node_type": "UserStory",
                "entities": [_entity_with_optional_fields(
                    "UserStory", first_line[:80], 0.90, description=text[:2000]
                )],
                "relationships": []
            }

        # Requirements
        if text.lower().startswith("system shall") or REQ_RE.search(text):
            req_id = REQ_RE.search(text)
            req_value = req_id.group(0) if req_id else first_line[:80]
            description = _extract_requirement_description_from_text(text, req_value)
            return {
                "block_category": "Requirement",
                "kg_node_type": "Requirement",
                "entities": [
                    _entity_with_optional_fields(
                        "Requirement",
                        req_value,
                        0.90,
                        description=description,
                    )
                ],
                "relationships": []
            }

        return None

    @staticmethod
    def _parse_kv_pairs(text: str) -> dict[str, str]:
        """Parse Key=Value or Key: Value pairs from a table row text."""
        return _parse_kv_pairs(text)

    # Header-pattern → (entity_type, block_category, regex_fallback)
    _HEADER_ENTITY_MAP: list[tuple[re.Pattern, str, str, re.Pattern | None]] = [
        (_TC_HEADER_PATTERNS, "TestCase", "TestCase", TC_RE),
        (_DEF_HEADER_PATTERNS, "Defect", "Defect", DEF_RE),
        (_INC_HEADER_PATTERNS, "Incident", "Incident", INC_RE),
        (_USERSTORY_HEADER_PATTERNS, "UserStory", "UserStory", None),
        (_REQ_HEADER_PATTERNS, "Requirement", "Requirement", REQ_RE),
        (_SCENARIO_HEADER_PATTERNS, "TestCase", "TestCase", SCENARIO_RE),
    ]

    # Primary entity type → which other entity types to cross-link and how
    _CROSS_LINK_MAP: dict[str, list[tuple[str, str]]] = {
        "TestCase": [
            ("Requirement", "COVERS"),
        ],
        "Defect": [
            ("TestCase", "MISSED_BY"),
            ("Requirement", "VIOLATES"),
        ],
        "Incident": [
            ("Process", "IMPACTS"),
        ],
        "UserStory": [
            ("Requirement", "IMPLEMENTS"),
        ],
    }

    _REFERENCE_PREFIX_RE = re.compile(
        r"^(?:linked|related|associated|parent|source)\b",
        re.IGNORECASE,
    )

    def _extract_structured_table_row(
        self, block: CanonicalBlock, text: str
    ) -> dict[str, Any] | None:
        """Extract entities from Key=Value structured table rows.

        Generic across all QE asset types: TestCase, Defect, Incident,
        Requirement, UserStory.  Detects the primary entity from column
        names, then cross-links to secondary entities found in the same row.
        Returns None if no recognisable ID column is found — falls through
        to LLM IE.
        """
        kv = self._parse_kv_pairs(text)
        if not kv:
            return None

        # Scan columns — distinguish primary ID columns from reference columns
        # "Defect ID" → primary; "Linked Test Case" → reference
        primary_found: dict[str, list[str]] = {}
        ref_found: dict[str, list[str]] = {}
        for col_name, col_val in kv.items():
            val = col_val.strip()
            if not val:
                continue
            is_ref = bool(self._REFERENCE_PREFIX_RE.search(col_name))
            target = ref_found if is_ref else primary_found
            for pattern, entity_type, _cat, _re_fb in self._HEADER_ENTITY_MAP:
                if pattern.search(col_name):
                    target.setdefault(entity_type, []).append(val)
                    break

        # Merge: all found entities (primary + ref) for cross-linking
        found: dict[str, list[str]] = {}
        for src in (primary_found, ref_found):
            for et, vals in src.items():
                found.setdefault(et, []).extend(vals)

        # Regex fallback: scan full text for ID patterns not found via headers
        _REGEX_FALLBACKS: list[tuple[str, re.Pattern]] = [
            ("TestCase", TC_RE),
            ("Requirement", REQ_RE),
            ("Defect", DEF_RE),
            ("Incident", INC_RE),
        ]
        for entity_type, regex in _REGEX_FALLBACKS:
            if entity_type not in found:
                matches = regex.findall(text)
                if matches:
                    found.setdefault(entity_type, []).extend(matches)

        if not found:
            return None

        # Determine primary entity — the first entity type found in a
        # non-reference (primary) column preserves spreadsheet column order.
        # Falls back to priority order for regex-only matches.
        primary_type: str | None = None
        primary_value: str | None = None
        if primary_found:
            first_type = next(iter(primary_found))
            primary_type = first_type
            primary_value = primary_found[first_type][0]
        else:
            _PRIORITY = ["TestCase", "Defect", "Incident", "UserStory", "Requirement"]
            for pt in _PRIORITY:
                if pt in found and found[pt]:
                    primary_type = pt
                    primary_value = found[pt][0]
                    break

        if not primary_type or not primary_value:
            return None

        primary_description: str | None = None
        if primary_type == "Requirement":
            primary_description = _description_from_kv_pairs(kv)
            if not primary_description:
                primary_description = _extract_requirement_description_from_text(
                    text,
                    primary_value,
                )

        entities: list[dict[str, Any]] = [
            _entity_with_optional_fields(
                primary_type,
                primary_value,
                0.99,
                description=primary_description,
            )
        ]
        relationships: list[dict[str, Any]] = []

        # Cross-link secondary entities
        cross_links = self._CROSS_LINK_MAP.get(primary_type, [])
        for secondary_type, rel_type in cross_links:
            for val in found.get(secondary_type, []):
                if val == primary_value:
                    continue
                entities.append({"type": secondary_type, "value": val, "confidence": 0.90})
                relationships.append({
                    "from_type": primary_type, "from_value": primary_value,
                    "rel_type": rel_type, "to_type": secondary_type, "to_value": val,
                    "confidence": 0.90,
                })

        # Category maps from primary entity type
        category_map = {
            "TestCase": "TestCase", "Defect": "Defect", "Incident": "Incident",
            "UserStory": "UserStory", "Requirement": "Requirement",
        }
        category = category_map.get(primary_type, "Narrative")

        spine_node = self._infer_spine_from_kv(kv)

        return {
            "block_category": category,
            "kg_node_type": primary_type,
            "spine_node": spine_node,
            "entities": entities,
            "relationships": relationships,
        }

    _SPINE_COL_RE = re.compile(
        r"sub[\s\-_]*module|module[\s\-_]*name|feature[\s\-_]*area|module[\s/]*feature",
        re.IGNORECASE,
    )

    def _infer_spine_from_kv(self, kv: dict[str, str]) -> str:
        """Pick the most specific spine-like column value from a table row's KV pairs.

        Preference order: sub-module > module > feature area.
        """
        sub_val = ""
        mod_val = ""
        for col, val in kv.items():
            v = val.strip()
            if not v:
                continue
            cl = col.lower()
            if "sub" in cl and "module" in cl:
                sub_val = v
            elif "module" in cl and "feature" not in cl:
                mod_val = v
            elif self._SPINE_COL_RE.search(col):
                if not mod_val:
                    mod_val = v
        return sub_val or mod_val or "domain_root"

    def _build_llm_input(
        self,
        block: CanonicalBlock,
        masked_text: str,
    ) -> str:
        """
        Build the LLM IE input.

        The document-level product context is prepended to every chunk so
        chunk-level extraction is interpreted in the context of the complete
        document.

        Table rows retain their existing header-aware context.
        """

        sections: list[str] = []

        # ------------------------------------------------------------
        # EXISTING TABLE-ROW CONTEXT
        # ------------------------------------------------------------

        if block.block_type != "table_row":
            sections.append(masked_text)
            return "\n".join(sections)

        header_candidate = (block.metadata or {}).get(
            "table_header_candidate"
        )
        row_index = (block.metadata or {}).get("table_row_index")
        header_is_self = (block.metadata or {}).get(
            "table_header_is_self"
        )

        lines: list[str] = []

        lines.append(
            "TABLE ROW CONTEXT: This is a row from a structured table."
        )

        if header_candidate and not header_is_self:
            lines.append(
                f"Table header (column names): {header_candidate}"
            )
            lines.append(f"Row index: {row_index}")
            lines.append(f"Row data: {masked_text}")
            lines.append("")
            lines.append("EXTRACTION INSTRUCTIONS:")
            lines.append(
                "- Each value is in ColumnName=Value format. "
                "Use column names to understand semantics."
            )
            lines.append(
                "- Extract all named entities (people, roles, systems, "
                "processes, controls, products, etc.)."
            )
            lines.append(
                "- Extract relationships between entities found in this row."
            )
            lines.append(
                "- Preserve structured identifiers (IDs, codes) as-is."
            )

        elif header_candidate and header_is_self:
            lines.append(
                "Table context: this may be the header row itself or a "
                "data row without a separate header."
            )
            lines.append(f"Row index: {row_index}")
            lines.append(f"Row text: {masked_text}")
            lines.append("")
            lines.append("EXTRACTION INSTRUCTIONS:")
            lines.append("- Extract all named entities from the text.")
            lines.append(
                "- Extract relationships between entities if discernible."
            )

        else:
            lines.append(f"Row data: {masked_text}")
            lines.append("")
            lines.append("EXTRACTION INSTRUCTIONS:")
            lines.append("- Extract all named entities from the text.")
            lines.append(
                "- Extract relationships between entities if discernible."
            )

        sections.append("\n".join(lines))

        return "\n".join(sections)

    def _ensure_entity_for_category(
        self,
        block_category: BlockCategory,
        kg_node_type: str,
        entities: list[dict[str, Any]],
        masked_text: str
    ) -> list[dict[str, Any]]:
        """Auto-create a safe entity for non-Narrative blocks with no entities."""
        # Skip entity creation for sentence-like content (UserStory, Requirement)
        # BusinessRule handled by rule-based extraction above
        # These should remain as semantic chunks only, not KG nodes
        if block_category in (BlockCategory.Narrative, BlockCategory.UserStory, BlockCategory.Requirement):
            return entities
        if not kg_node_type:
            return entities
        if entities:
            return entities
        seed = (masked_text or "").strip()
        if not seed:
            return entities
        first_line = seed.splitlines()[0].strip()
        if "|" in first_line:
            first_line = first_line.split("|", 1)[0].strip()
        value = first_line[:160] if first_line else seed[:160]
        if not value:
            return entities
        return [{
            "type": kg_node_type,
            "value": value,
            "confidence": 0.75,
            "auto_generated": True,
        }]

    @staticmethod
    def _build_alias_variants(value: str) -> list[str]:
        """Deterministic alias variants for ID-like values (safe, low-noise)."""
        if not value or not isinstance(value, str):
            return []
        v = value.strip()
        if not v:
            return []
        # Only generate variants for ID-like tokens (digits + separators)
        if not re.search(r"\d", v):
            return []
        variants: list[str] = []
        # Hyphen/space normalization
        no_sep = re.sub(r"[\s_-]+", "", v)
        spaced = re.sub(r"[_-]+", " ", v)
        if no_sep and no_sep != v:
            variants.append(no_sep)
        if spaced and spaced != v:
            variants.append(spaced)
        # Uppercase canonical
        upper = v.upper()
        if upper != v:
            variants.append(upper)
        # De-dupe and drop the original
        cleaned: list[str] = []
        seen = set([v.lower()])
        for cand in variants:
            c = cand.strip()
            if not c:
                continue
            key = c.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(c)
        return cleaned

    def _extract_id_signals(self, text: str) -> list[dict[str, Any]]:
        """Extract ID-based entity signals without overriding semantic IE."""
        signals: list[dict[str, Any]] = []
        if not text:
            return signals
        patterns = [
            (INC_RE, "Incident"),
            (DEF_RE, "Defect"),
            (TC_RE, "TestCase"),
            (REQ_RE, "Requirement"),
            (AC_RE, "AcceptanceCriteria"),
            (SCENARIO_RE, "TestScenario"),
        ]
        for regex, entity_type in patterns:
            for match in regex.findall(text):
                signal = {
                    "type": entity_type,
                    "value": match,
                    "confidence": 0.85,
                    "id_signal": True,
                    "aliases": self._build_alias_variants(match)
                }
                description = (
                    _extract_requirement_description_from_text(text, match)
                    if entity_type == "Requirement"
                    else _extract_entity_description_from_text(text, entity_type, match)
                )
                if description:
                    signal["description"] = description
                signals.append(signal)
        return signals

    def _augment_entities_with_id_signals(
        self,
        entities: list[dict[str, Any]],
        masked_text: str
    ) -> list[dict[str, Any]]:
        """Add ID-signal entities when IE misses them (non-invasive)."""
        if not masked_text:
            return entities
        existing = {(e.get("type", "").lower(), str(e.get("value", "")).lower()) for e in entities if isinstance(e, dict)}
        for signal in self._extract_id_signals(masked_text):
            key = (signal.get("type", "").lower(), str(signal.get("value", "")).lower())
            if key in existing:
                continue
            entities.append(signal)
            existing.add(key)
        return entities

    def _suggest_aliases(self, entity_type: str, entity_value: str, source_text: str) -> list[str]:
        cfg = get_app_config()
        alias_cfg = cfg.get("ingestion", {}).get("aliases", {})
        if not alias_cfg.get("enabled", True):
            return []
        max_aliases = int(alias_cfg.get("max_per_entity", 5))
        if not entity_value:
            return []
        cache_key = (entity_type or "", entity_value)
        if cache_key in self._alias_cache:
            return self._alias_cache[cache_key][:max_aliases]
        text_lower = (source_text or "").lower()
        prompt = (
            "Return a JSON array of short aliases or abbreviations for the entity below. "
            "Only return aliases that would reasonably appear in the same document.\n\n"
            f"Entity type: {entity_type}\n"
            f"Entity value: {entity_value}\n"
        )
        aliases: list[str] = []
        try:
            import requests
            response = requests.post(
                f"{self.llm_client.base_url}/api/generate",
                json={
                    "model": self.llm_client.text_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 128}
                },
                timeout=self.llm_client.timeout_sec
            )
            response.raise_for_status()
            response_text = (response.json().get("response") or "").strip()
            json_start = response_text.find("[")
            json_end = response_text.rfind("]")
            if json_start != -1 and json_end != -1:
                aliases = json.loads(response_text[json_start:json_end + 1])
        except Exception:
            aliases = []
        # Validate aliases by presence or abbreviation
        def _abbr(value: str) -> str:
            parts = re.findall(r"[A-Za-z]+", value)
            return "".join(p[0] for p in parts).upper()
        abbr = _abbr(entity_value)
        valid = []
        for a in aliases:
            if not isinstance(a, str):
                continue
            aa = a.strip()
            if not aa or aa.lower() == entity_value.lower():
                continue
            if aa.lower() in text_lower or aa.upper() == abbr:
                valid.append(aa)
        # Add deterministic ID variants (safe normalization)
        for v in self._build_alias_variants(entity_value):
            if v not in valid:
                valid.append(v)
        valid = valid[:max_aliases]
        self._alias_cache[cache_key] = valid
        return valid

    def _resolve_entity_aliases(
        self,
        entity: dict[str, Any],
        source_text: str,
    ) -> list[str]:
        """
        Resolve aliases for an entity: use IE aliases when present (validated/capped),
        else call _suggest_aliases only when aliases.enabled.
        """
        cfg = get_app_config()
        alias_cfg = cfg.get("ingestion", {}).get("aliases", {})
        max_aliases = int(alias_cfg.get("max_per_entity", 5))
        entity_value = entity.get("value", "")
        entity_type = entity.get("type", "")

        # Use IE aliases when present
        raw = entity.get("aliases")
        if isinstance(raw, list) and raw:
            valid: list[str] = []
            seen: set[str] = set()
            value_lower = (entity_value or "").lower()
            for a in raw:
                if not isinstance(a, str):
                    continue
                aa = a.strip()
                if not aa or aa.lower() == value_lower:
                    continue
                key = aa.lower()
                if key in seen:
                    continue
                seen.add(key)
                valid.append(aa)
                if len(valid) >= max_aliases:
                    break
            return valid

        # No IE aliases: call _suggest_aliases only when enabled
        if not alias_cfg.get("enabled", True):
            return []
        return self._suggest_aliases(entity_type, entity_value, source_text)

    @staticmethod
    def _parse_swift_field(field_name: str) -> tuple[str | None, str | None]:
        """
        Parse SWIFT-like field codes from extracted field names.
        Examples:
          - "F38A Tolerance +/-" -> ("F38A", "Tolerance +/-")
          - "F31C Date" -> ("F31C", "Date")
        """
        if not field_name:
            return None, None
        m = re.match(r"^\s*(F\d{2}[A-Z])\s*[-:]*\s*(.*)\s*$", field_name.strip(), flags=re.IGNORECASE)
        if not m:
            return None, field_name.strip()
        code = (m.group(1) or "").upper().strip()
        label = (m.group(2) or "").strip() or None
        return code or None, label

    @staticmethod
    def _build_extracted_field_semantic_text(
        field_name: str,
        field_value: str,
        source_file: str,
        domain: str
    ) -> str:
        """
        Build a richer, natural-language searchable representation of an extracted form field.

        Goal: make natural-language queries like "swift code for tolerance percentage" retrieve the right field block
        (without relying on query-time heuristics).
        """
        field_name = (field_name or "").strip()
        field_value = (field_value or "").strip()

        code, label = IEClient._parse_swift_field(field_name)

        # Minimal curated alias/meaning dictionary (extend over time)
        aliases_by_code: dict[str, list[str]] = {
            "F38A": ["tolerance", "tolerance +/-", "tolerance percentage", "percentage tolerance", "+/- tolerance"],
            "F31C": ["date", "issue date", "date of issue"],
            "F32B": ["amount", "lc amount", "credit amount"],
            "F43P": ["partial shipment", "partial shipments"],
            "F45A": ["goods description", "description of goods"],
            "F50": ["applicant"],
            "F59": ["beneficiary"],
            "F58A": ["confirming bank"],
        }

        # Compose a semantic "document" for this field block
        parts: list[str] = []

        if code:
            parts.append(f"SWIFT MT700 field code {code}.")
            parts.append(f"SWIFT code {code}.")  # explicit phrase match
        else:
            parts.append("SWIFT MT700 form field.")

        if label:
            parts.append(f"Field name: {label}.")
        elif field_name:
            parts.append(f"Field name: {field_name}.")

        # Add canonical domain/doc context (helps disambiguation + natural language matches)
        if domain:
            parts.append(f"Domain: {domain}.")
        if source_file:
            parts.append(f"Document: {source_file}.")
        parts.append("Context: Letter of Credit (LC) application / agreement form.")

        # Add field value if present
        if field_value:
            parts.append(f"Captured value: {field_value}.")

        # Add aliases/meaning
        if code and code.upper() in aliases_by_code:
            aliases = aliases_by_code[code.upper()]
            parts.append("Also referred to as: " + ", ".join(aliases) + ".")

        # Always include original extracted text for traceability
        if field_name and field_value:
            parts.append(f"Extracted: {field_name}: {field_value}.")
        elif field_name:
            parts.append(f"Extracted: {field_name}.")

        return " ".join(p.strip() for p in parts if p and p.strip())

    def _is_form_image(self, ie_result) -> bool:
        """Detect if a vision IE result represents a data capture form (not a process diagram)."""
        extracted_fields = getattr(ie_result, "extracted_fields", None) or []
        extracted_knowledge = getattr(ie_result, "extracted_knowledge", None) or []
        entities = ie_result.entities or []

        process_actor_count = sum(
            1 for e in entities
            if e.get("type") in ("Process", "Actor", "Activity", "Decision", "Gateway")
        )
        field_like_count = sum(
            1 for e in entities
            if e.get("type") in ("DataEntity", "System", "Component", "FormCapture", "Form")
        )
        if process_actor_count > field_like_count:
            return False

        has_many_fields = len(extracted_fields) >= 3
        has_form_fields = any(
            k.get("type") == "form_field" for k in extracted_knowledge
        )

        return has_many_fields and (has_form_fields or field_like_count >= 2)

    def _collapse_form_entities(self, ie_result, mapped_entities, mapped_relationships):
        """Collapse form image entities into a single FormCapture node."""
        form_title = None
        for e in mapped_entities:
            if e.get("type") in ("FormCapture", "Form", "Process") and "form" in e.get("value", "").lower():
                form_title = e["value"]
                break
        if not form_title:
            desc = getattr(ie_result, "description", "") or ""
            if "form" in desc.lower():
                form_title = desc[:80]
            else:
                form_title = "Data Capture Form"

        fields = {}
        for f in (getattr(ie_result, "extracted_fields", None) or []):
            fields[f.get("field_name", f.get("name", ""))] = f.get("value", "")

        form_entity = {
            "type": "FormCapture",
            "value": form_title,
            "confidence": 0.90,
            "fields": fields,
            "aliases": [],
        }

        kept_entities = [form_entity]
        kept_relationships = []

        for e in mapped_entities:
            if e.get("type") in ("Control", "Rule", "Regulation"):
                kept_entities.append(e)
                logger.info(
                    "FormCapture retained governance entity without invalid "
                    "GOVERNED_BY edge: form=%s entity_type=%s entity=%s",
                    form_title,
                    e.get("type"),
                    e.get("value"),
                )

        for k in (getattr(ie_result, "extracted_knowledge", None) or []):
            if k.get("type") == "business_rule":
                content = k.get("content", "")[:80]
                rule_entity = {"type": "Rule", "value": content, "confidence": 0.80, "aliases": []}
                kept_entities.append(rule_entity)
                logger.info(
                    "FormCapture retained extracted business rule without invalid "
                    "GOVERNED_BY edge: form=%s rule=%s",
                    form_title,
                    content,
                )

        return kept_entities, kept_relationships, form_title


    def enrich_blocks(
        self,
        blocks: list[CanonicalBlock],
        progress_cb: Any | None = None,
        domain: str = "default",
        skip_text_ie: bool = False
    ) -> list[EnrichedBlock]:
        """
        Enrich canonical blocks with IE results, embeddings, and PII masking.
        
        Args:
            blocks: List of CanonicalBlock instances
            
        Returns:
            List of EnrichedBlock instances (always returns a list, never None)
        """
        def _progress(phase: str, pct: int, **counts):
            if not progress_cb:
                return
            try:
                progress_cb(phase, pct, **counts)
            except Exception:
                # Progress updates must never break ingestion
                return

        # Validate input
        if blocks is None:
            self.logger.warning("blocks parameter is None, returning empty list")
            return []
        if not isinstance(blocks, list):
            self.logger.warning(f"blocks parameter is not a list (type: {type(blocks)}), returning empty list")
            return []
        if not blocks:
            return []
        
        # Split into text and image blocks
        text_blocks: list[CanonicalBlock] = []
        image_blocks: list[CanonicalBlock] = []
        
        for idx, block in enumerate(blocks):
            if block is None:
                self.logger.warning(f"Found None block at index {idx}, skipping")
                continue
            if not hasattr(block, 'image_bytes') or not hasattr(block, 'text'):
                self.logger.warning(f"Block at index {idx} missing required attributes, skipping")
                continue
            if block.image_bytes is not None:
                image_blocks.append(block)
            elif block.text:
                text_blocks.append(block)
        
        enriched_blocks: list[EnrichedBlock] = []
        self._failure_causes = {}  # Reset for this run

        # Initialize entity standardizer for this domain
        entity_standardizer = EntityStandardizer(domain=domain)

        # Process text blocks
        if text_blocks:
            _progress("PII Masking (Text)", 25)
            # PII masking with dual-mode strategy (redacted for audit, faker for LLM/retrieval)
            redacted_texts: list[str] = []  # For audit logs
            masked_texts: list[str] = []  # For LLM/retrieval (faker)
            pii_spans_list: list[list[dict[str, Any]]] = []
            
            for block in text_blocks:
                text_redacted, text_faker, pii_spans = self.pii_service.mask_text(block.text)
                redacted_texts.append(text_redacted)  # Redacted version for audit
                masked_texts.append(text_faker)  # Faker version for LLM
                pii_spans_list.append(pii_spans)

            # PII masking summary (counts only; no raw or masked text in server logs)
            pii_type_counts: dict[str, int] = {}
            total_pii_spans = 0
            for spans in pii_spans_list:
                for span in (spans or []):
                    if isinstance(span, dict):
                        ptype = span.get("entity_type", "UNKNOWN")
                        pii_type_counts[ptype] = pii_type_counts.get(ptype, 0) + 1
                        total_pii_spans += 1
            self.logger.info(
                "ingestion.enrichment.pii",
                domain=domain,
                text_block_count=len(text_blocks),
                pii_span_count=total_pii_spans,
                blocks_with_pii=sum(1 for s in pii_spans_list if s),
                pii_type_counts=pii_type_counts,
            )

            # Rule-based IE (deterministic) for structured sections
            rule_based_results: list[dict[str, Any] | None] = []
            for block in text_blocks:
                rule_based_results.append(self._rule_based_ie(block))
            self.logger.info(
                "ingestion.enrichment.rule_based",
                domain=domain,
                text_block_count=len(text_blocks),
                rule_based_hits=sum(1 for rb in rule_based_results if rb),
            )

            # LLM text IE — two_pass (Lane2) or single_pass (legacy one-shot)
            # Track lane per text block for existing-node mapping (lane1=rule, lane2=LLM)
            text_ie_lanes: list[str] = ["lane2"] * len(text_blocks)
            if skip_text_ie:
                print(f"[IE] SKIPPING text IE processing (--skip-text-ie flag enabled)", flush=True)
                self.logger.info("Text IE skipped", block_count=len(text_blocks), reason="--skip-text-ie flag")
                # Create default IE results with empty entities/relationships
                text_ie_results = []
                for idx, rb in enumerate(rule_based_results):
                    if rb:
                        text_ie_results.append(rb)
                        text_ie_lanes[idx] = "lane1"
                    else:
                        text_ie_results.append({
                            "block_category": "Narrative",
                            "kg_node_type": None,
                            "entities": [],
                            "relationships": []
                        })
                batch_padding_count = 0
            else:
                _progress("LLM IE (Text)", 35)
                text_ie_mode = self._resolve_text_ie_mode()
                text_ie_results: list[dict[str, Any] | None] = [None] * len(text_blocks)
                llm_indices: list[int] = []
                for idx, (masked_text, block, rb) in enumerate(zip(masked_texts, text_blocks, rule_based_results)):
                    if rb:
                        text_ie_results[idx] = rb
                        text_ie_lanes[idx] = "lane1"
                    else:
                        llm_indices.append(idx)
                        text_ie_lanes[idx] = "lane2"

                if llm_indices:
                    self.logger.info(
                        "Text IE mode selected",
                        mode=text_ie_mode,
                        llm_block_count=len(llm_indices),
                        rule_based_count=sum(1 for rb in rule_based_results if rb),
                    )
                    print(
                        f"[IE] Text IE mode={text_ie_mode} "
                        f"({len(llm_indices)} LLM blocks, "
                        f"{sum(1 for rb in rule_based_results if rb)} rule-based)",
                        flush=True,
                    )
                    if text_ie_mode == "two_pass":
                        llm_blocks = [text_blocks[i] for i in llm_indices]
                        llm_masked = [masked_texts[i] for i in llm_indices]
                        llm_results, batch_padding_count = self._batch_lane2_two_pass_ie(
                            llm_blocks,
                            llm_masked,
                            progress_cb=_progress,
                        )
                    else:
                        llm_inputs = [
                            (self._build_llm_input(text_blocks[i], masked_texts[i]), text_blocks[i].domain)
                            for i in llm_indices
                        ]
                        llm_results, batch_padding_count = self._batch_text_ie(
                            llm_inputs, progress_cb=_progress
                        )
                    for idx, res in zip(llm_indices, llm_results):
                        text_ie_results[idx] = res
                else:
                    batch_padding_count = 0
                # Fill any remaining gaps with defaults
                for idx, res in enumerate(text_ie_results):
                    if res is None:
                        text_ie_results[idx] = {
                            "block_category": "Narrative",
                            "kg_node_type": None,
                            "entities": [],
                            "relationships": []
                        }

            # Validate and parse IE results
            validated_results: list[IEResult] = []
            ie_success_count = 0
            ie_failure_count = 0
            
            for idx, (block, ie_result) in enumerate(zip(text_blocks, text_ie_results)):
                try:
                    validated = IEResult(**ie_result)
                    validated_results.append(validated)
                    ie_success_count += 1
                except Exception as e:
                    ie_failure_count += 1
                    self._failure_causes["text_ie_validation_error"] = self._failure_causes.get("text_ie_validation_error", 0) + 1
                    self.logger.warning(
                        "Invalid IE result, using defaults",
                        error=str(e),
                        error_type=type(e).__name__,
                        result=ie_result,
                        block_id=block.block_id,
                        block_index=idx,
                        source_file=block.source_file
                    )
                    try:
                        validated_results.append(IEResult(
                            block_category="Narrative",
                            kg_node_type=None,
                            entities=[],
                            relationships=[]
                        ))
                    except Exception as fallback_err:
                        self.logger.error(
                            "Fallback IEResult also failed — appending bare-minimum stub",
                            fallback_error=str(fallback_err),
                            block_id=block.block_id,
                        )
                        validated_results.append(IEResult.model_construct(
                            block_category="Narrative",
                            kg_node_type=None,
                            entities=[],
                            relationships=[]
                        ))
            
            # Log IE success/failure summary
            total_blocks = len(text_blocks)
            success_rate = (ie_success_count / total_blocks * 100) if total_blocks > 0 else 0
            self.logger.info(
                "IE processing summary",
                total_blocks=total_blocks,
                ie_validation_successful=ie_success_count,
                ie_validation_failed=ie_failure_count,
                batch_level_padding=batch_padding_count,
                success_rate_pct=round(success_rate, 2)
            )
            
            # DIAGNOSTIC: Log entity/relationship extraction totals from LLM
            total_entities_from_llm = sum(len(r.entities) for r in validated_results)
            total_relationships_from_llm = sum(len(r.relationships) for r in validated_results)
            print(f"[IE][DIAG] LLM extraction totals: {total_entities_from_llm} entities, {total_relationships_from_llm} relationships from {len(validated_results)} blocks", flush=True)
            self.logger.info(
                "IE extraction totals from LLM",
                total_entities=total_entities_from_llm,
                total_relationships=total_relationships_from_llm,
                block_count=len(validated_results)
            )
            
            # DIAGNOSTIC: Sample first 3 non-empty entity extractions
            sample_extractions = []
            for idx, r in enumerate(validated_results[:20]):  # Check first 20
                if r.entities or r.relationships:
                    sample_extractions.append({
                        "block_idx": idx,
                        "category": r.block_category,
                        "entities": r.entities[:3],  # First 3
                        "relationships": r.relationships[:2]  # First 2
                    })
                    if len(sample_extractions) >= 3:
                        break
            
            if sample_extractions:
                print(f"[IE][DIAG] Sample extractions: {sample_extractions}", flush=True)
                self.logger.info("IE sample extractions", samples=sample_extractions)
            else:
                print(f"[IE][DIAG] WARNING: No entities or relationships extracted from first 20 blocks!", flush=True)
                self.logger.warning("No entities or relationships extracted from LLM", checked_blocks=min(20, len(validated_results)))
            
            # Embeddings
            _progress("Embedding (Text)", 55)
            print(f"[IE] Generating embeddings for {len(masked_texts)} texts...", flush=True)
            # Log embedding creation for audit
            for idx, (block, masked_text) in enumerate(zip(text_blocks, masked_texts)):
                self.logger.info(
                    "Creating embedding for text block",
                    block_id=block.block_id[:8],
                    text_length=len(masked_text),
                    model="Embeddings"
                )
            embeddings = self.llm_client.embed_text(masked_texts)
            embedding_dim = len(embeddings[0]) if embeddings else None
            print(f"[IE] Embeddings generated: {len(embeddings)} vectors ({embedding_dim} dimensions each)", flush=True)
            
            # Build EnrichedBlock instances
            category_failures = 0
            for idx, (block, text_redacted, masked_text, pii_spans, ie_result, embedding, lane) in enumerate(zip(
                text_blocks, redacted_texts, masked_texts, pii_spans_list, validated_results, embeddings, text_ie_lanes
            )):
                try:
                    # Validate block
                    if block is None:
                        self.logger.warning(f"Text block at index {idx} is None, skipping")
                        continue
                    if ie_result is None:
                        self.logger.warning(f"IE result at index {idx} is None, skipping")
                        continue
                    if embedding is None:
                        self.logger.warning(f"Embedding at index {idx} is None, using empty vector")
                        embedding = []
                    
                    # Validate and convert block category
                    try:
                        block_category = BlockCategory(ie_result.block_category)
                    except (ValueError, AttributeError, TypeError) as e:
                        category_failures += 1
                        self._failure_causes["category_validation_failure"] = self._failure_causes.get("category_validation_failure", 0) + 1
                        self.logger.warning(
                            "Invalid block category from IE, defaulting to Narrative",
                            category=getattr(ie_result, 'block_category', 'MISSING'),
                            block_id=getattr(block, 'block_id', f'index_{idx}'),
                            source_file=getattr(block, 'source_file', 'unknown'),
                            error=str(e),
                            available_categories=[bc.value for bc in BlockCategory]
                        )
                        block_category = BlockCategory.Narrative
                    
                    # Validate kg_node_type
                    kg_node_type = getattr(ie_result, 'kg_node_type', None) or None
                    if kg_node_type and not isinstance(kg_node_type, str):
                        kg_node_type = None

                    spine_node = getattr(ie_result, 'spine_node', None) or "domain_root"
                    
                    # Validate entities and relationships
                    entities = getattr(ie_result, 'entities', [])
                    if not isinstance(entities, list):
                        entities = []
                    relationships = getattr(ie_result, 'relationships', [])
                    if not isinstance(relationships, list):
                        relationships = []
                    
                    # Filter low-confidence entities and relationships
                    # LLM sometimes hallucinates entities; low confidence = noise in KG
                    CONFIDENCE_THRESHOLD = 0.7
                    entities_before = len(entities)
                    relationships_before = len(relationships)
                    
                    entities = [e for e in entities 
                                if isinstance(e, dict) and e.get("confidence", 1.0) >= CONFIDENCE_THRESHOLD]
                    relationships = [r for r in relationships 
                                     if isinstance(r, dict) and r.get("confidence", 1.0) >= CONFIDENCE_THRESHOLD]
                    
                    # DIAGNOSTIC: Log confidence filtering (always log first few)
                    if idx < 5 or entities_before != len(entities) or relationships_before != len(relationships):
                        self.logger.info(
                            "IE confidence filtering",
                            block_idx=idx,
                            block_id=getattr(block, 'block_id', f'idx_{idx}')[:8],
                            entities_before=entities_before,
                            entities_after=len(entities),
                            relationships_before=relationships_before,
                            relationships_after=len(relationships),
                            threshold=CONFIDENCE_THRESHOLD
                        )
                        if idx < 3:
                            print(f"[IE][DIAG] Block {idx}: entities {entities_before}->{len(entities)}, rels {relationships_before}->{len(relationships)} (threshold={CONFIDENCE_THRESHOLD})", flush=True)

                    _SPINE_TYPES = {"DomainRoot", "ProductLine", "Function", "SubFunction"}
                    spine_dupes = [e for e in entities if isinstance(e, dict) and e.get("type") in _SPINE_TYPES]
                    if spine_dupes:
                        entities = [e for e in entities if not (isinstance(e, dict) and e.get("type") in _SPINE_TYPES)]
                        relationships = [
                            r for r in relationships
                            if not (isinstance(r, dict) and (
                                r.get("from_type") in _SPINE_TYPES or r.get("to_type") in _SPINE_TYPES
                            ))
                        ]

                    # ONTOLOGY ADMISSION GATE
                    #
                    # LLM/IE output is candidate data only. Nothing proceeds
                    # into the KG pipeline until it passes the authoritative
                    # ontology admission engine.
                    #
                    # This is deliberately ontology-wide. Evidence gates,
                    # node vocabulary, relationship vocabulary, endpoint
                    # compatibility, and confidence are enforced centrally.
                    admission_report = self.ontology_admission.admit(
                        entities,
                        relationships,
                        source_text=masked_text,
                        block_category=block_category.value,
                    )

                    entities = admission_report.admitted_entities
                    relationships = admission_report.admitted_relationships

                    entity_rejected = len(admission_report.rejected_entities)
                    relationship_rejected = len(
                        admission_report.rejected_relationships
                    )

                    if entity_rejected or relationship_rejected:
                        self.logger.info(
                            "ontology.admission.filter",
                            block_idx=idx,
                            block_id=getattr(
                                block, "block_id", f"index_{idx}"
                            )[:8],
                            source_file=getattr(
                                block, "source_file", "unknown"
                            ),
                            block_category=block_category.value,
                            entities_before=entities_before,
                            entities_admitted=len(entities),
                            entities_rejected=entity_rejected,
                            relationships_before=relationships_before,
                            relationships_admitted=len(relationships),
                            relationships_rejected=relationship_rejected,
                            entity_rejection_reasons=(
                                admission_report.entity_rejection_reasons
                            ),
                            relationship_rejection_reasons=(
                                admission_report.relationship_rejection_reasons
                            ),
                            admission_engine="ontology_admission_v1",
                        )

                        print(
                            f"[IE][ADMISSION] Block {idx}: "
                            f"entities {entities_before}->{len(entities)}, "
                            f"relationships {relationships_before}->{len(relationships)}; "
                            f"rejected entities={entity_rejected}, "
                            f"relationships={relationship_rejected}",
                            flush=True,
                        )

                    # RELATIONSHIP INFERENCE: Enhance relationships using ontology rules
                    # This is a SAFE enhancement - only adds relationships, never removes
                    inferred_relationships = self._infer_relationships(
                        entities, relationships, domain
                    )
                    if inferred_relationships:
                        relationships.extend(inferred_relationships)
                        self.logger.info(
                            "Relationship inference added",
                            block_idx=idx,
                            block_id=getattr(block, 'block_id', f'idx_{idx}')[:8],
                            original_relationships=relationships_before,
                            inferred_relationships=len(inferred_relationships),
                            total_relationships=len(relationships)
                        )
                    
                    # Validate pii_spans
                    if not isinstance(pii_spans, list):
                        pii_spans = []
                    
                    # Validate masked_text
                    if not isinstance(masked_text, str):
                        masked_text = getattr(block, 'text', '')
                    
                    # Validate embedding
                    if not isinstance(embedding, list):
                        embedding = []
                    
                    # Standardize entity names for consistency
                    standardized_entities = []
                    for entity in entities:
                        standardized_value = entity_standardizer.standardize_entity(
                            entity.get("type", ""),
                            entity.get("value", "")
                        )
                        standardized_entity = entity.copy()
                        standardized_entity["value"] = standardized_value
                        standardized_entity["aliases"] = self._resolve_entity_aliases(
                            standardized_entity,
                            masked_text
                        )
                        standardized_entities.append(standardized_entity)

                    # Standardize relationship entity references
                    standardized_relationships = []
                    for rel in relationships:
                        standardized_rel = rel.copy()
                        # Standardize from_value and to_value entity references
                        if "from_value" in rel:
                            from_type = rel.get("from_type", "")
                            standardized_rel["from_value"] = entity_standardizer.standardize_entity(
                                from_type, rel["from_value"]
                            )
                        if "to_value" in rel:
                            to_type = rel.get("to_type", "")
                            standardized_rel["to_value"] = entity_standardizer.standardize_entity(
                                to_type, rel["to_value"]
                            )
                        standardized_relationships.append(standardized_rel)

                    # Auto-create KG node entity for non-Narrative categories when empty
                    standardized_entities = self._ensure_entity_for_category(
                        block_category, kg_node_type, standardized_entities, masked_text
                    )
                    # Add ID-signal entities as non-invasive backstop
                    standardized_entities = self._augment_entities_with_id_signals(
                        standardized_entities, masked_text
                    )

                    # Shared existing-node mapping (Lane 1 rule-based / Lane 2 LLM)
                    standardized_entities = self._apply_entity_mapping(
                        lane=lane,
                        block=block,
                        entities=standardized_entities,
                        relationships=standardized_relationships,
                        source_context=masked_text,
                    )

                    # FINAL ONTOLOGY ADMISSION
                    #
                    # Downstream enrichment is allowed to create/augment
                    # candidates, so the first admission gate is not enough.
                    # This final boundary guarantees that nothing reaches
                    # EnrichedBlock unless the complete candidate set still
                    # conforms to the authoritative ontology.
                    final_admission_report = self.ontology_admission.admit(
                        standardized_entities,
                        standardized_relationships,
                        source_text=masked_text,
                        block_category=block_category.value,
                    )

                    standardized_entities = (
                        final_admission_report.admitted_entities
                    )
                    standardized_relationships = (
                        final_admission_report.admitted_relationships
                    )

                    final_entity_rejected = len(
                        final_admission_report.rejected_entities
                    )
                    final_relationship_rejected = len(
                        final_admission_report.rejected_relationships
                    )

                    if final_entity_rejected or final_relationship_rejected:
                        self.logger.info(
                            "ontology.admission.final_filter",
                            block_idx=idx,
                            block_id=getattr(
                                block, "block_id", f"index_{idx}"
                            )[:8],
                            source_file=getattr(
                                block, "source_file", "unknown"
                            ),
                            block_category=block_category.value,
                            final_entities_admitted=len(
                                standardized_entities
                            ),
                            final_entities_rejected=final_entity_rejected,
                            final_relationships_admitted=len(
                                standardized_relationships
                            ),
                            final_relationships_rejected=(
                                final_relationship_rejected
                            ),
                            entity_rejection_reasons=(
                                final_admission_report.entity_rejection_reasons
                            ),
                            relationship_rejection_reasons=(
                                final_admission_report.relationship_rejection_reasons
                            ),
                            admission_engine="ontology_admission_v1",
                        )

                        print(
                            f"[IE][FINAL_ADMISSION] Block {idx}: "
                            f"entities admitted={len(standardized_entities)}, "
                            f"rejected={final_entity_rejected}; "
                            f"relationships admitted="
                            f"{len(standardized_relationships)}, "
                            f"rejected={final_relationship_rejected}",
                            flush=True,
                        )

                    # Create EnrichedBlock with dual-mode PII fields
                    enriched_block = EnrichedBlock(
                        block=block,
                        block_category=block_category,
                        kg_node_type=kg_node_type,
                        spine_node=spine_node,
                        candidate_entities=standardized_entities,
                        candidate_relationships=standardized_relationships,
                        embedding=embedding,
                        pii_spans=pii_spans,
                        original_text_redacted=text_redacted,  # For audit logs
                        masked_text=masked_text,  # For retrieval/LLM (faker)
                    )
                    enriched_blocks.append(enriched_block)
                except Exception as e:
                    self.logger.exception(
                        "Failed to create EnrichedBlock",
                        error=str(e),
                        block_index=idx,
                        block_id=getattr(block, 'block_id', 'unknown') if block else 'None',
                        source_file=getattr(block, 'source_file', 'unknown') if block else 'None'
                    )
                    # Continue with next block
            
            # Log category validation summary
            if category_failures > 0:
                self.logger.warning(
                    "Block category validation failures",
                    failure_count=category_failures,
                    total_blocks=len(text_blocks),
                    failure_rate_pct=round(category_failures / len(text_blocks) * 100, 2)
                )
        
        # Process image blocks
        self.logger.info(
            "Image block detection",
            total_blocks=len(blocks),
            image_blocks_count=len(image_blocks),
            text_blocks_count=len(text_blocks),
            image_block_ids=[b.block_id for b in image_blocks[:5]]  # Log first 5 IDs
        )
        
        # Note: Image blocks with None/empty image_bytes are now filtered out before building image_items
        # (check happens at line 530-534)
        
        if image_blocks:
            _progress("Vision IE (Images)", 60)
            print(f"[IE] Processing {len(image_blocks)} image blocks", flush=True)
            
            # Note: Vision API now uses /api/generate only (proven to work in standalone tests)
            # Health check removed - /api/generate is reliable
            
            # Build context_text from nearest heading
            # Group by source_file and find headings
            file_to_blocks: dict[str, list[CanonicalBlock]] = {}
            for block in blocks:
                if block.source_file not in file_to_blocks:
                    file_to_blocks[block.source_file] = []
                file_to_blocks[block.source_file].append(block)
            
            image_items: list[dict[str, Any]] = []
            image_audit: list[dict[str, Any]] = []  # parallel to image_items/image_blocks
            for img_block in image_blocks:
                # Skip image blocks with None/empty image_bytes (likely EMF conversion failed)
                if not img_block.image_bytes:
                    self._failure_causes["vision_ie_skipped_no_data"] = self._failure_causes.get("vision_ie_skipped_no_data", 0) + 1
                    self.logger.warning("Skipping image block - no image data (likely EMF conversion failed)", block_id=img_block.block_id[:8], source_file=img_block.source_file)
                    print(f"[IE] Skipping image block {img_block.block_id[:8]} - no image data (likely EMF conversion failed)", flush=True)
                    image_audit.append({})  # Keep 1:1 with image_blocks for indexing
                    continue
                
                # Find nearest heading in same file (prefer semantic grouping metadata if present)
                heading_context_raw = ""
                try:
                    heading_context_raw = ((img_block.metadata or {}).get("heading_context_raw") or "").strip()
                except Exception:
                    heading_context_raw = ""
                if not heading_context_raw and img_block.source_file in file_to_blocks:
                    file_blocks = file_to_blocks[img_block.source_file]
                    # Find heading with lower block_index
                    headings = [
                        b for b in file_blocks
                        if b.block_type == "heading" and b.block_index < img_block.block_index
                    ]
                    if headings:
                        # Use most recent heading
                        nearest_heading = max(headings, key=lambda b: b.block_index)
                        heading_context_raw = nearest_heading.text[:200]  # Limit length

                # Prefer caption extracted deterministically from DOCX XML (if present on image metadata)
                caption_raw = ""
                try:
                    caption_raw = ((img_block.metadata or {}).get("caption_text_raw") or "").strip()
                except Exception:
                    caption_raw = ""

                # Fallback: Find nearby caption paragraph (often immediately preceding an image)
                if img_block.source_file in file_to_blocks:
                    file_blocks = file_to_blocks[img_block.source_file]
                    paragraphs = [
                        b for b in file_blocks
                        if b.block_type == "paragraph" and b.block_index < img_block.block_index and (b.text or "").strip()
                    ]
                    if paragraphs:
                        nearest_para = max(paragraphs, key=lambda b: b.block_index)
                        # Heuristic: caption is usually within 1-2 blocks before the image and short
                        if not caption_raw and (img_block.block_index - nearest_para.block_index) <= 2 and len(nearest_para.text) <= 200:
                            caption_raw = nearest_para.text[:200]
                
                # Mask text that will be sent to the LLM (enterprise posture: prefer masked inputs)
                _, heading_context_masked, _ = self.pii_service.mask_text(heading_context_raw) if heading_context_raw else ("", "", [])
                _, caption_masked, _ = self.pii_service.mask_text(caption_raw) if caption_raw else ("", "", [])

                # Combine masked context for the vision prompt
                context_text = heading_context_masked
                if caption_masked:
                    context_text = (context_text + " | " if context_text else "") + f"Caption: {caption_masked}"

                # Mandatory strip context when image is part of a tiled diagram (so LLM uses consistent names for merge)
                meta = img_block.metadata or {}
                strip_idx = meta.get("tile_strip_index")
                strip_total = meta.get("tile_strip_total")
                if strip_total is not None and strip_total > 0:
                    strip_n = strip_idx if strip_idx is not None else "?"
                    strip_ctx = f"This image is strip {strip_n} of {strip_total} of the same diagram. Use consistent short names for components so strips can be merged."
                    context_text = (strip_ctx + "\n\n" + context_text) if context_text else strip_ctx

                # Inject Tesseract OCR text (pre-extracted from native smask composite in parser.py).
                # Prepending detected text lets the Vision LM verify or fill entity names it cannot
                # read clearly at the reduced resolution, and gives gateway/actor labels as hints.
                ocr_text = (img_block.metadata or {}).get("ocr_text", "")
                if ocr_text:
                    # Pass the full OCR text — the Vision LM context window is large (~128k tokens)
                    # and trimming at 2500 chars was hiding entity labels that appear later in the
                    # OCR scan (e.g. right-hand swimlane headers, bottom gateway labels).
                    ocr_prefix = f"DETECTED TEXT IN IMAGE (from Tesseract OCR scan):\n{ocr_text}\n\n"
                    context_text = ocr_prefix + (context_text or "")
                    print(
                        f"[IE] OCR context injected for block {img_block.block_id[:8]}: "
                        f"{len(ocr_text)} chars (full, no truncation)",
                        flush=True,
                    )

                image_items.append({
                    "image_bytes": img_block.image_bytes,
                    "domain": img_block.domain,
                    "context_text": context_text,
                })

                image_audit.append({
                    "heading_context_raw": heading_context_raw,
                    "heading_context_masked": heading_context_masked,
                    "diagram_caption_text_raw": caption_raw,
                    "diagram_caption_text_masked": caption_masked,
                })
            
            # LLM vision IE with graceful fallback (batch processing for large image sets)
            try:
                _progress("LLM IE (Vision)", 65)
                vision_start = time.time()

                # Batch vision IE processing (configurable batch size from config)
                config = get_app_config()
                vision_batch_size = config.get("ollama", {}).get("vision", {}).get("batch_size", 4)
                all_image_ie_results = []

                if len(image_items) <= vision_batch_size:
                    # Small batch - process all at once
                    all_image_ie_results = self.llm_client.ie_image_analysis(image_items, ontology=self._runtime_ontology)
                else:
                    # Large batch - process in configurable chunks
                    self.logger.info(
                        "Processing large image set in batches",
                        total_images=len(image_items),
                        batch_size=vision_batch_size,
                        num_batches=(len(image_items) + vision_batch_size - 1) // vision_batch_size
                    )

                    for i in range(0, len(image_items), vision_batch_size):
                        batch_items = image_items[i:i + vision_batch_size]
                        batch_results = self.llm_client.ie_image_analysis(batch_items, ontology=self._runtime_ontology)

                        if len(batch_results) != len(batch_items):
                            self.logger.warning(
                                "Vision IE batch result count mismatch",
                                batch_start=i,
                                expected=len(batch_items),
                                got=len(batch_results)
                            )

                        all_image_ie_results.extend(batch_results)

                        # Configurable pause between batches to avoid overwhelming the LLM
                        if i + vision_batch_size < len(image_items):
                            batch_delay = config.get("ollama", {}).get("vision", {}).get("batch_delay_seconds", 10.0)
                            time.sleep(batch_delay)

                image_ie_results = all_image_ie_results
                vision_duration_ms = (time.time() - vision_start) * 1000.0
                self.logger.info(
                    "Vision IE completed",
                    image_count=len(image_ie_results),
                    expected_count=len(image_blocks),
                    duration_ms=round(vision_duration_ms, 1)
                )
            except Exception as vision_err:
                # Graceful fallback: use default descriptions instead of failing ingestion
                self._failure_causes["vision_ie_model_failure"] = self._failure_causes.get("vision_ie_model_failure", 0) + len(image_blocks)
                import traceback
                error_details = {
                    "error_type": type(vision_err).__name__,
                    "error_message": str(vision_err),
                    "traceback": traceback.format_exc(),
                    "image_count": len(image_blocks),
                    "image_sources": [img.source_file for img in image_blocks],
                    "image_sizes": [len(img.image_bytes) if img.image_bytes else 0 for img in image_blocks]
                }
                
                self.logger.error(
                    "Vision IE failed - detailed diagnostics",
                    **error_details
                )
                print(
                    f"[IE] VISION MODEL FAILURE\n"
                    f"  Error Type: {error_details['error_type']}\n"
                    f"  Error: {error_details['error_message']}\n"
                    f"  Images Affected: {error_details['image_count']}\n"
                    f"  Sources: {', '.join(error_details['image_sources'])}\n"
                    f"  See logs for full traceback",
                    flush=True
                )
                
                # Create default results for all images
                # NOTE: Store error info in description for report visibility
                image_ie_results = [
                    {
                        "description": (
                            f"Image description unavailable (Vision IE failed: {error_details['error_type']}). "
                            f"Source: {img_block.source_file}"
                        ),
                        "block_category": "Diagram",
                        "kg_node_type": None,
                        "entities": [],
                        "relationships": [],
                        "vision_error": str(vision_err)  # Store for debugging
                    }
                    for img_block in image_blocks
                ]
                vision_duration_ms = 0.0
            
            # Validate and parse IE results
            validated_image_results: list[ImageIEResult] = []
            for idx, ie_result in enumerate(image_ie_results):
                try:
                    validated = ImageIEResult(**ie_result)
                    validated_image_results.append(validated)
                except ValidationError as e:
                    self._failure_causes["vision_ie_validation_error"] = self._failure_causes.get("vision_ie_validation_error", 0) + 1
                    # Log full validation error with diagnostic info
                    import json
                    self.logger.error(
                        "Vision IE validation failed",
                        block_id=image_blocks[idx].block_id if idx < len(image_blocks) else "unknown",
                        error=str(e),
                        llm_response_preview=json.dumps(ie_result)[:500] if isinstance(ie_result, dict) else str(ie_result)[:500],
                        image_index=idx,
                        exc_info=True
                    )
                    
                    # Try to extract vision fields from failed response if they exist
                    extracted_fields = ie_result.get("extracted_fields", []) if isinstance(ie_result, dict) else []
                    extracted_knowledge = ie_result.get("extracted_knowledge", []) if isinstance(ie_result, dict) else []
                    semantic_chunks = ie_result.get("semantic_chunks", []) if isinstance(ie_result, dict) else []
                    api_method = ie_result.get("api_method", "error") if isinstance(ie_result, dict) else "error"
                    vision_model_used = ie_result.get("vision_model_used") if isinstance(ie_result, dict) else None
                    
                    # Ensure lists are actually lists
                    if not isinstance(extracted_fields, list):
                        extracted_fields = []
                    if not isinstance(extracted_knowledge, list):
                        extracted_knowledge = []
                    if not isinstance(semantic_chunks, list):
                        semantic_chunks = []
                    
                    validated_image_results.append(ImageIEResult(
                        description=f"Image description unavailable (validation error: {str(e)})",
                        block_category="Diagram",
                        kg_node_type=None,
                        entities=[],
                        relationships=[],
                        extracted_fields=extracted_fields,
                        extracted_knowledge=extracted_knowledge,
                        semantic_chunks=semantic_chunks,
                        api_method=api_method,
                        vision_model_used=vision_model_used
                    ))
            
            # Ensure we have one result per image block
            if len(validated_image_results) != len(image_blocks):
                self.logger.warning(
                    "Image IE result count mismatch",
                    expected=len(image_blocks),
                    got=len(validated_image_results)
                )
                # Pad with defaults if needed
                while len(validated_image_results) < len(image_blocks):
                    validated_image_results.append(ImageIEResult(
                        description="Image description unavailable (missing result)",
                        block_category="Diagram",
                        kg_node_type=None,
                        entities=[],
                        relationships=[],
                        extracted_fields=[],
                        extracted_knowledge=[],
                        semantic_chunks=[],
                        vision_model_used=None
                    ))
                # Truncate if too many
                validated_image_results = validated_image_results[:len(image_blocks)]
            
            # Generate embeddings for image descriptions + extracted_fields + extracted_knowledge
            _progress("Embedding (Images)", 70)
            # Per-block combined text (description + fields + knowledge)
            combined_texts = []
            for idx, result in enumerate(validated_image_results):
                text_parts = []
                if result.description:
                    text_parts.append(result.description)
                if getattr(result, "extracted_fields", None):
                    fields_text = " ".join([
                        f"{field.get('field_name', '')}: {field.get('field_value', '')}"
                        for field in result.extracted_fields
                        if field.get('field_value')
                    ])
                    if fields_text:
                        text_parts.append(f"Fields: {fields_text}")
                if getattr(result, "extracted_knowledge", None):
                    knowledge_text = " ".join([
                        item.get('knowledge_item', '') or item.get('description', '')
                        for item in result.extracted_knowledge
                        if item.get('knowledge_item') or item.get('description')
                    ])
                    if knowledge_text:
                        text_parts.append(f"Knowledge: {knowledge_text}")
                combined_text = " | ".join(text_parts) if text_parts else result.description or ""
                combined_texts.append(combined_text)
                self.logger.info(
                    "Creating embedding for image block",
                    block_id=image_blocks[idx].block_id[:8] if idx < len(image_blocks) else "unknown",
                    text_length=len(combined_text),
                    model="Embeddings",
                    has_fields=bool(getattr(result, "extracted_fields", None)),
                    has_knowledge=bool(getattr(result, "extracted_knowledge", None))
                )

            # One chunk per image: group tiled strips into single units; one embedding per unit
            output_units = _build_tiled_output_units(image_blocks)
            combined_texts_per_unit = []
            for unit in output_units:
                if len(unit) == 1:
                    combined_texts_per_unit.append(combined_texts[unit[0]])
                else:
                    combined_texts_per_unit.append("\n\n".join(combined_texts[i] for i in unit))
            print(f"[IE] Generating embeddings for {len(combined_texts_per_unit)} image units ({len(image_blocks)} blocks, tiled merge applied)...", flush=True)
            embeddings_per_unit = self.llm_client.embed_text(combined_texts_per_unit)
            embedding_dim = len(embeddings_per_unit[0]) if embeddings_per_unit else None
            print(f"[IE] Embeddings generated: {len(embeddings_per_unit)} vectors ({embedding_dim} dimensions each)", flush=True)

            per_image_duration_ms = (vision_duration_ms / len(image_blocks)) if image_blocks and vision_duration_ms else None
            extra_blocks: list[tuple[CanonicalBlock, BlockCategory, str, str, str]] = []
            extra_texts_for_embedding: list[str] = []

            for unit_idx, unit in enumerate(output_units):
                # Resolve to one block and one result (merged for tiled, else single)
                if len(unit) == 1:
                    img_block = image_blocks[unit[0]]
                    ie_result = validated_image_results[unit[0]]
                    audit = image_audit[unit[0]] if unit[0] < len(image_audit) else {}
                else:
                    img_block, ie_result = _merge_tiled_image_results(
                        [image_blocks[i] for i in unit],
                        [validated_image_results[i] for i in unit],
                    )
                    audit = image_audit[unit[0]] if unit[0] < len(image_audit) else {}
                embedding = embeddings_per_unit[unit_idx] if unit_idx < len(embeddings_per_unit) else (embeddings_per_unit[0] if embeddings_per_unit else [])
                try:
                    block_category = BlockCategory(ie_result.block_category)
                except ValueError:
                    self.logger.warning(
                        "Invalid block category from image IE",
                        category=ie_result.block_category,
                        block_id=img_block.block_id
                    )
                    block_category = BlockCategory.Diagram
                
                # Store vision description with dual-mode PII masking
                vision_description_raw = ie_result.description or ""
                vision_description_redacted, vision_description_masked, vision_pii_spans = self.pii_service.mask_text(vision_description_raw) if vision_description_raw else ("", "", [])

                # Update block text with RAW description (audit), keep image_bytes as-is
                block_dict = img_block.model_dump()
                block_dict["text"] = vision_description_raw
                updated_block = CanonicalBlock(**block_dict)
                
                # Use vision description if available, otherwise placeholder (dual-mode)
                if vision_description_masked and vision_description_masked != "Image description unavailable":
                    final_masked_text = vision_description_masked
                    final_redacted_text = vision_description_redacted
                else:
                    final_masked_text = "Image description unavailable"
                    final_redacted_text = "[REDACTED_IMAGE_UNAVAILABLE]"
                    self.logger.warning(
                        "No content available for image (vision IE failed)",
                        block_id=updated_block.block_id,
                        source_file=updated_block.source_file
                    )
                
                # Vision IE type mapping: map vision-specific types to main ontology
                vision_mapping = self._run_context.ontology.domain_definition.get("vision_ie_type_mapping", {})
                entity_type_map = vision_mapping.get("entity_types", {})
                rel_type_map = vision_mapping.get("relationship_types", {})

                mapped_entities = []
                for entity in ie_result.entities:
                    etype = entity.get("type", "")
                    mapped_type = entity_type_map.get(etype, etype)
                    if mapped_type == "_WEAVIATE_ONLY":
                        continue
                    entity_copy = entity.copy()
                    entity_copy["type"] = mapped_type
                    mapped_entities.append(entity_copy)

                mapped_relationships = []
                for rel in ie_result.relationships:
                    rtype = rel.get("rel_type", "")
                    mapped_rel_type = rel_type_map.get(rtype, rtype)
                    if mapped_rel_type == "_EXTRACTED_KNOWLEDGE":
                        continue
                    rel_copy = rel.copy()
                    rel_copy["rel_type"] = mapped_rel_type
                    if rel_copy.get("from_type") in entity_type_map:
                        from_mapped = entity_type_map[rel_copy["from_type"]]
                        if from_mapped == "_WEAVIATE_ONLY":
                            continue
                        rel_copy["from_type"] = from_mapped
                    if rel_copy.get("to_type") in entity_type_map:
                        to_mapped = entity_type_map[rel_copy["to_type"]]
                        if to_mapped == "_WEAVIATE_ONLY":
                            continue
                        rel_copy["to_type"] = to_mapped
                    mapped_relationships.append(rel_copy)

                # Post-mapping direction/type fixups for vision IE relationships.
                # The vision model emits PERFORMED_BY as Process→Actor which maps
                # to PERFORMS but keeps the wrong direction; flip so Actor→Process.
                # Process→System with PERFORMS should become USES instead.
                _PROCESS_LIKE = {"Process"}
                _ACTOR_LIKE = {"Actor"}
                _SYSTEM_LIKE = {"System"}
                for rel in mapped_relationships:
                    rt = rel.get("rel_type", "")
                    ft = rel.get("from_type", "")
                    tt = rel.get("to_type", "")
                    if rt == "PERFORMS" and ft in _PROCESS_LIKE and tt in _ACTOR_LIKE:
                        rel["from_type"], rel["to_type"] = rel["to_type"], rel["from_type"]
                        rel["from_value"], rel["to_value"] = rel["to_value"], rel["from_value"]
                    elif rt == "PERFORMS" and ft in _PROCESS_LIKE and tt in _SYSTEM_LIKE:
                        rel["rel_type"] = "USES"

                # Standardize entity names for consistency
                standardized_entities = []
                for entity in mapped_entities:
                    standardized_value = entity_standardizer.standardize_entity(
                        entity.get("type", ""),
                        entity.get("value", "")
                    )
                    standardized_entity = entity.copy()
                    standardized_entity["value"] = standardized_value
                    standardized_entity["aliases"] = self._resolve_entity_aliases(
                        standardized_entity,
                        final_masked_text or ""
                    )
                    standardized_entities.append(standardized_entity)

                standardized_entities = self._ensure_entity_for_category(
                    block_category, ie_result.kg_node_type, standardized_entities, final_masked_text
                )

                # Standardize relationship entity references
                standardized_relationships = []
                for rel in mapped_relationships:
                    standardized_rel = rel.copy()
                    if "from_value" in rel:
                        from_type = rel.get("from_type", "")
                        standardized_rel["from_value"] = entity_standardizer.standardize_entity(
                            from_type, rel["from_value"]
                        )
                    if "to_value" in rel:
                        to_type = rel.get("to_type", "")
                        standardized_rel["to_value"] = entity_standardizer.standardize_entity(
                            to_type, rel["to_value"]
                        )
                    standardized_relationships.append(standardized_rel)

                # Promote business_rule extracted_knowledge items to KG Rule entities + GOVERNED_BY rels
                _EK_DECISION_KW = ("not eligible", "if ", "must ", "require", "condition", "eligible")
                for ek_item in (getattr(ie_result, "extracted_knowledge", None) or []):
                    if not isinstance(ek_item, dict):
                        continue
                    if ek_item.get("type") != "business_rule" and ek_item.get("category") != "operational":
                        continue
                    content = ek_item.get("content", "")
                    if not any(kw in content.lower() for kw in _EK_DECISION_KW):
                        continue
                    rule_value = entity_standardizer.standardize_entity("Rule", content[:80])
                    rule_entity = {
                        "type": "Rule",
                        "value": rule_value,
                        "confidence": ek_item.get("confidence", 0.80),
                        "aliases": [],
                    }
                    standardized_entities.append(rule_entity)
                    for ref in (ek_item.get("entities_referenced") or []):
                        for existing in standardized_entities:
                            if existing.get("type") == "Process" and existing.get("value") == ref:
                                standardized_relationships.append({
                                    "from_type": "Process",
                                    "from_value": existing["value"],
                                    "rel_type": "GOVERNED_BY",
                                    "to_type": "Rule",
                                    "to_value": rule_value,
                                    "confidence": 0.80,
                                })
                                break

                # Form detection: collapse field-heavy images into a single FormCapture node
                if self._is_form_image(ie_result):
                    standardized_entities, standardized_relationships, _form_title = self._collapse_form_entities(
                        ie_result, standardized_entities, standardized_relationships
                    )
                    block_category = BlockCategory.FormCapture
                    _form_fields = {}
                    for f in (getattr(ie_result, "extracted_fields", None) or []):
                        _form_fields[f.get("field_name", f.get("name", ""))] = f.get("value", "")
                    structured_text = f"Form: {_form_title}"
                    for fname, fval in _form_fields.items():
                        structured_text += f" | {fname}: {fval}"
                    for ek in (getattr(ie_result, "extracted_knowledge", None) or []):
                        ek_content = (ek.get("content") or ek.get("description") or "").strip()
                        if ek_content:
                            structured_text += f" | Knowledge: {ek_content}"
                    final_redacted_text, final_masked_text, _ = self.pii_service.mask_text(structured_text)
                    self.logger.info(
                        "Form image detected – collapsed to FormCapture",
                        block_id=img_block.block_id,
                        form_title=_form_title,
                        field_count=len(_form_fields),
                        entity_count=len(standardized_entities),
                    )

                # Shared existing-node mapping (Lane 3 vision — no Pass 1 classification)
                standardized_entities = self._apply_entity_mapping(
                    lane="lane3",
                    block=updated_block,
                    entities=standardized_entities,
                    relationships=standardized_relationships,
                )

                enriched_blocks.append(EnrichedBlock(
                    block=updated_block,
                    block_category=block_category,
                    kg_node_type=ie_result.kg_node_type,
                    spine_node=getattr(ie_result, 'spine_node', None) or "domain_root",
                    candidate_entities=standardized_entities,
                    candidate_relationships=standardized_relationships,
                    embedding=embedding,
                    pii_spans=vision_pii_spans,
                    original_text_redacted=final_redacted_text,  # For audit logs
                    masked_text=final_masked_text,  # For retrieval/LLM (faker)
                    vision_description_raw=vision_description_raw or None,
                    vision_description_masked=vision_description_masked or None,
                    diagram_caption_text_raw=audit.get("diagram_caption_text_raw") or None,
                    diagram_caption_text_masked=audit.get("diagram_caption_text_masked") or None,
                    vision_model=getattr(ie_result, "vision_model_used", None) or getattr(self.llm_client, "vision_model", None),
                    vision_duration_ms=per_image_duration_ms,
                    extracted_fields=getattr(ie_result, "extracted_fields", None),
                    extracted_knowledge=getattr(ie_result, "extracted_knowledge", None),
                    semantic_chunks=getattr(ie_result, "semantic_chunks", None),
                    vision_api_method=getattr(ie_result, "api_method", "unknown")
                ))

                # Per-field Weaviate blocks disabled — fields are already captured in
                # the combined FormCapture chunk text. Re-enable when fine-grained
                # field search is needed independently of the parent form.
                _EMIT_PER_FIELD_BLOCKS = False
                fields = getattr(ie_result, "extracted_fields", None) or []
                if _EMIT_PER_FIELD_BLOCKS and len(unit) == 1 and isinstance(fields, list) and fields:
                    for f_idx, field in enumerate(fields):
                        if not isinstance(field, dict):
                            continue
                        field_name = (field.get("field_name") or field.get("name") or "").strip()
                        field_value = (field.get("field_value") or field.get("value") or "").strip()
                        if not field_name and not field_value:
                            continue

                        # Canonical text for retrieval (rich semantic representation; provenance goes to metadata)
                        field_text_raw = self._build_extracted_field_semantic_text(
                            field_name=field_name,
                            field_value=field_value,
                            source_file=img_block.source_file,
                            domain=img_block.domain,
                        )
                        field_redacted, field_masked, _ = self.pii_service.mask_text(field_text_raw) if field_text_raw else ("", "", [])
                        field_code, field_label = self._parse_swift_field(field_name)

                        # Stable-ish ordering: anchor under parent image block_index
                        derived_block_index = int(img_block.block_index) * 1000 + 100 + f_idx
                        # Stable ID uses parent image block_id + field name + index (avoid collisions)
                        stable_for_id = f"{img_block.block_id}|field|{f_idx}|{field_name}"
                        derived_block_id = generate_block_id(img_block.source_file, derived_block_index, img_block.domain, content=stable_for_id)

                        field_block = CanonicalBlock(
                            block_id=derived_block_id,
                            source_file=img_block.source_file,
                            file_type=img_block.file_type,
                            domain=img_block.domain,
                            page_number=img_block.page_number,
                            block_index=derived_block_index,
                            block_type="extracted_field",
                            text=field_text_raw,
                            image_bytes=None,
                            semantic_unit_id=derived_block_id,
                            source_block_ids=[derived_block_id],
                            unit_type="extracted_field",
                            unit_part_index=1,
                            unit_part_total=1,
                            metadata={
                                "source": "vision_extracted_field",
                                "parent_block_id": img_block.block_id,
                                "parent_block_index": img_block.block_index,
                                "field_name": field_name,
                                "field_label": field_label,
                                "field_code": field_code,
                                "mt_message_type": "MT700",
                            }
                        )
                        extra_blocks.append((field_block, BlockCategory.Reference, None, field_redacted, field_masked))
                        extra_texts_for_embedding.append(field_masked or field_text_raw)

                # Per-knowledge Weaviate blocks disabled (same flag as per-field)
                knowledge_items = getattr(ie_result, "extracted_knowledge", None) or []
                if _EMIT_PER_FIELD_BLOCKS and len(unit) == 1 and isinstance(knowledge_items, list) and knowledge_items:
                    for k_idx, item in enumerate(knowledge_items):
                        if not isinstance(item, dict):
                            continue
                        knowledge_text_raw = (
                            item.get("knowledge_item")
                            or item.get("description")
                            or item.get("text")
                            or ""
                        ).strip()
                        if not knowledge_text_raw:
                            continue
                        k_redacted, k_masked, _ = self.pii_service.mask_text(knowledge_text_raw)
                        derived_block_index = int(img_block.block_index) * 1000 + 500 + k_idx
                        stable_for_id = f"{img_block.block_id}|knowledge|{k_idx}|{knowledge_text_raw[:80]}"
                        derived_block_id = generate_block_id(img_block.source_file, derived_block_index, img_block.domain, content=stable_for_id)
                        k_block = CanonicalBlock(
                            block_id=derived_block_id,
                            source_file=img_block.source_file,
                            file_type=img_block.file_type,
                            domain=img_block.domain,
                            page_number=img_block.page_number,
                            block_index=derived_block_index,
                            block_type="extracted_knowledge",
                            text=knowledge_text_raw,
                            image_bytes=None,
                            semantic_unit_id=derived_block_id,
                            source_block_ids=[derived_block_id],
                            unit_type="extracted_knowledge",
                            unit_part_index=1,
                            unit_part_total=1,
                            metadata={
                                "source": "vision_extracted_knowledge",
                                "parent_block_id": img_block.block_id,
                                "parent_block_index": img_block.block_index,
                            }
                        )
                        extra_blocks.append((k_block, BlockCategory.Reference, None, k_redacted, k_masked))
                        extra_texts_for_embedding.append(k_masked or knowledge_text_raw)

            # Embed and append extra blocks (first-class citizens)
            if extra_blocks:
                print(f"[IE] Generating embeddings for {len(extra_blocks)} extracted field/knowledge blocks...", flush=True)
                extra_embeddings = self.llm_client.embed_text(extra_texts_for_embedding)
                for (blk, cat, node_type, redacted, masked), emb in zip(extra_blocks, extra_embeddings):
                    entity_value = (blk.metadata or {}).get("field_name") if node_type == "FormField" else blk.text
                    enriched_blocks.append(EnrichedBlock(
                        block=blk,
                        block_category=cat,
                        kg_node_type=node_type,
                        candidate_entities=[
                            {
                                "type": node_type,
                                "value": entity_value or blk.text,
                                "confidence": 0.99
                            }
                        ] if node_type in ("FormField", "Rule") else [],
                        candidate_relationships=[],
                        embedding=emb,
                        pii_spans=[],
                        original_text_redacted=redacted,
                        masked_text=masked or blk.text,
                    ))
            
            # Log semantic chunks created from images
            for img_block, ie_result in zip(image_blocks, validated_image_results):
                semantic_chunks = getattr(ie_result, "semantic_chunks", None) or []
                if semantic_chunks:
                    print(f"[IE][Vision] Image {img_block.block_id[:8]} created {len(semantic_chunks)} semantic chunks:", flush=True)
                    for idx, chunk in enumerate(semantic_chunks[:5], 1):  # Show first 5
                        chunk_type = chunk.get("chunk_type", "unknown")
                        chunk_text = chunk.get("text", "")[:100]  # First 100 chars
                        print(f"  [{idx}] {chunk_type}: {chunk_text}...", flush=True)
                    if len(semantic_chunks) > 5:
                        print(f"  ... and {len(semantic_chunks) - 5} more chunks", flush=True)
                else:
                    print(f"[IE][Vision] Image {img_block.block_id[:8]} created 0 semantic chunks", flush=True)
        
        return enriched_blocks

    def _resolve_text_ie_mode(self) -> str:
        """Return ``two_pass`` or ``single_pass`` from config (default: two_pass)."""
        try:
            mode = (
                get_app_config()
                .get("ingestion", {})
                .get("text_ie", {})
                .get("mode", "two_pass")
            )
        except Exception:
            mode = "two_pass"
        mode_norm = str(mode or "two_pass").strip().lower().replace("-", "_")
        if mode_norm in {"single", "single_pass", "legacy", "one_shot", "oneshot"}:
            return "single_pass"
        return "two_pass"

    def _batch_lane2_two_pass_ie(
        self,
        blocks: list[CanonicalBlock],
        masked_texts: list[str],
        progress_cb: Any | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Run Lane2 via reusable ``run_lane2_ie`` utility; return IEResult-shaped dicts.

        Mapping runs later in enrich_blocks via ``step_map_entities`` so all lanes
        share one mapping call site. Embeddings/PII remain in enrich_blocks.
        """
        from tqdm import tqdm

        from kbfabric_ingest.ingest.lane2_two_pass import lane2_result_to_ie_dict
        from kbfabric_ingest.ingest.pipeline import run_lane2_ie

        if self._run_context is None:
            raise RuntimeError("IEClient run context has not been configured")

        if len(blocks) != len(masked_texts):
            raise ValueError(
                f"blocks/masked_texts length mismatch: {len(blocks)} vs {len(masked_texts)}"
            )

        config = get_app_config()
        delay_sec = float(
            config.get("ollama", {}).get("text", {}).get("batch_delay_seconds", 2.0) or 0.0
        )

        deps = self._get_pipeline_deps()
        results: list[dict[str, Any]] = []
        failures = 0
        default_result = {
            "block_category": "Narrative",
            "kg_node_type": None,
            "spine_node": "domain_root",
            "entities": [],
            "relationships": [],
        }

        total = len(blocks)
        self.logger.info(
            "Lane2 two-pass text IE starting",
            total_blocks=total,
            delay_seconds=delay_sec,
            via_utility="run_lane2_ie",
        )
        print(f"[IE] Lane2 two-pass: processing {total} blocks (utility: run_lane2_ie)", flush=True)

        def _progress(phase: str, pct: int, **counts: Any) -> None:
            if not progress_cb:
                return
            try:
                progress_cb(phase, pct, **counts)
            except Exception:
                return

        with tqdm(total=total, desc="Lane2 two-pass", unit="block", leave=True, ncols=100) as pbar:
            for i, (block, masked_text) in enumerate(zip(blocks, masked_texts)):
                pbar.set_description(f"Lane2 {i + 1}/{total}")
                pct = 35 + int(20 * ((i + 1) / max(total, 1)))
                _progress("LLM IE (Text two-pass)", min(pct, 54), blocks_done=i + 1, blocks_total=total)
                try:
                    print(
                        f"[IE][Lane2][{i + 1}/{total}] INPUT "
                        f"block={getattr(block, 'block_id', '')[:8]} "
                        f"file={getattr(block, 'source_file', '?')} "
                        f"type={getattr(block, 'block_type', '?')} "
                        f"chars={len(masked_text or '')}",
                        flush=True,
                    )
                    two_pass = run_lane2_ie(
                        block,
                        deps,
                        masked_text=masked_text,
                    )
                    ie_dict = lane2_result_to_ie_dict(two_pass, self._runtime_ontology)
                    ents = ie_dict.get("entities") or []
                    rels = ie_dict.get("relationships") or []
                    print(
                        f"[IE][Lane2][{i + 1}/{total}] OUTPUT "
                        f"success={two_pass.success} "
                        f"category={ie_dict.get('block_category')} "
                        f"categories={two_pass.classification.identified_categories} "
                        f"kg_node={ie_dict.get('kg_node_type')} "
                        f"entities={len(ents)} rels={len(rels)} "
                        f"extract_calls={len(two_pass.extractions)} "
                        f"pass1_ms={two_pass.classification.llm_duration_ms} "
                        f"total_ms={two_pass.total_duration_ms:.1f}",
                        flush=True,
                    )
                    self.logger.info(
                        "ingestion.enrichment.lane2.block",
                        block_id=getattr(block, "block_id", None),
                        source_file=getattr(block, "source_file", None),
                        success=two_pass.success,
                        failure_reason=two_pass.failure_reason,
                        block_category=ie_dict.get("block_category"),
                        kg_node_type=ie_dict.get("kg_node_type"),
                        entity_count=len(ents),
                        relationship_count=len(rels),
                        extraction_call_count=len(two_pass.extractions),
                        pass1_category=two_pass.classification.category,
                        pass1_categories=two_pass.classification.identified_categories,
                        pass1_confidence=two_pass.classification.confidence,
                        pass2_invoked=two_pass.pass2_invoked,
                        total_duration_ms=two_pass.total_duration_ms,
                        via_utility="run_lane2_ie",
                    )
                    if not two_pass.success:
                        failures += 1
                        self._failure_causes["lane2_two_pass_failure"] = (
                            self._failure_causes.get("lane2_two_pass_failure", 0) + 1
                        )
                        self.logger.warning(
                            "Lane2 two-pass incomplete for block",
                            block_id=getattr(block, "block_id", None),
                            source_file=getattr(block, "source_file", None),
                            failure_reason=two_pass.failure_reason,
                            pass1_category=two_pass.classification.category,
                            pass2_invoked=two_pass.pass2_invoked,
                        )
                    results.append(ie_dict)
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    self._failure_causes["lane2_two_pass_exception"] = (
                        self._failure_causes.get("lane2_two_pass_exception", 0) + 1
                    )
                    self.logger.exception(
                        "Lane2 two-pass raised; using Narrative default",
                        block_id=getattr(block, "block_id", None),
                        source_file=getattr(block, "source_file", None),
                        error=str(exc),
                    )
                    results.append(dict(default_result))

                pbar.update(1)
                if delay_sec > 0 and i + 1 < total:
                    time.sleep(delay_sec)

        self.logger.info(
            "Lane2 two-pass text IE completed",
            total_blocks=total,
            failures=failures,
            entities=sum(len(r.get("entities") or []) for r in results),
            relationships=sum(len(r.get("relationships") or []) for r in results),
            via_utility="run_lane2_ie",
        )
        print(
            f"[IE] Lane2 two-pass done: {total} blocks, {failures} failures/incomplete",
            flush=True,
        )
        # batch_padding_count stays 0 — two-pass does not pad batch slots
        return results, 0
    
    def _batch_text_ie(
        self,
        text_ie_inputs: list[tuple[str, str]],
        progress_cb: Any | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Batch text IE calls to respect max batch size, preserving order.

        progress_cb: callable(phase: str, pct: int, **counts)
        """
        from kbfabric_ingest.ingest.config import get_app_config
        
        config = get_app_config()
        batch_size = config.get("ollama", {}).get("text", {}).get("batch_size", 8)
        
        all_results: list[dict[str, Any]] = []
        total_batches = (len(text_ie_inputs) + batch_size - 1) // batch_size
        total_batch_padding = 0  # Track total blocks that needed default padding
        batches_skipped = 0
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 10
        HTTP_500_RETRY_DELAYS = [5, 15]
        
        self.logger.info(
            "Batching text IE calls",
            total_blocks=len(text_ie_inputs),
            batch_size=batch_size,
            total_batches=total_batches
        )
        print(f"[IE] Processing {len(text_ie_inputs)} blocks in {total_batches} batches (max {batch_size} blocks per batch)", flush=True)
        
        # Process in batches with progress bar
        from tqdm import tqdm
        import requests as _requests
        batch_range = range(0, len(text_ie_inputs), batch_size)
        overall_start_time = time.time()
        
        with tqdm(total=total_batches, desc="IE Batches", unit="batch", leave=True, ncols=100) as pbar:
            for batch_idx in batch_range:
                batch = text_ie_inputs[batch_idx:batch_idx + batch_size]
                batch_num = (batch_idx // batch_size) + 1
                
                pbar.set_description(f"IE Batch {batch_num}/{total_batches}")
                print(f"[IE] Processing batch {batch_num}/{total_batches} ({len(batch)} blocks)", flush=True)
                
                batch_start_time = time.time()
                batch_results = None
                batch_text_chars = sum(safe_len(t) for (t, _d) in batch)
                self.logger.info(
                    "ingestion.enrichment.batch",
                    phase="start",
                    batch_id=batch_num,
                    total_batches=total_batches,
                    batch_size=len(batch),
                    text_chars_total=batch_text_chars,
                )

                for attempt in range(1 + len(HTTP_500_RETRY_DELAYS)):
                    try:
                        batch_results = self.llm_client.ie_text_analysis(batch, self._runtime_ontology)
                        consecutive_failures = 0
                        break
                    except (_requests.exceptions.HTTPError, RuntimeError) as exc:
                        status = None
                        if isinstance(exc, _requests.exceptions.HTTPError) and exc.response is not None:
                            status = exc.response.status_code
                        elif "500" in str(exc):
                            status = 500
                        
                        if status == 500 and attempt < len(HTTP_500_RETRY_DELAYS):
                            delay = HTTP_500_RETRY_DELAYS[attempt]
                            print(f"[IE] Batch {batch_num} got HTTP 500, retrying in {delay}s (attempt {attempt + 2}/{1 + len(HTTP_500_RETRY_DELAYS)})...", flush=True)
                            self.logger.warning(
                                "HTTP 500 on text IE batch, retrying",
                                batch_num=batch_num,
                                attempt=attempt + 1,
                                delay_s=delay,
                            )
                            time.sleep(delay)
                            continue
                        
                        # All retries exhausted or non-500 error
                        consecutive_failures += 1
                        batches_skipped += 1
                        self.logger.error(
                            "Text IE batch failed after retries, skipping",
                            batch_num=batch_num,
                            error=str(exc),
                            consecutive_failures=consecutive_failures,
                        )
                        print(f"[IE] Batch {batch_num} FAILED after retries — skipping ({consecutive_failures} consecutive failures)", flush=True)
                        
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            raise RuntimeError(
                                f"Text IE halted: {MAX_CONSECUTIVE_FAILURES} consecutive batch failures. "
                                f"Last error: {exc}"
                            ) from exc
                        break
                
                batch_succeeded = batch_results is not None
                if batch_results is None:
                    default_result = {
                        "block_category": "Narrative",
                        "kg_node_type": None,
                        "entities": [],
                        "relationships": [],
                    }
                    batch_results = [default_result.copy() for _ in range(len(batch))]
                    total_batch_padding += len(batch)
                
                batch_duration = time.time() - batch_start_time
                self.logger.info(
                    "ingestion.enrichment.batch",
                    phase="end",
                    batch_id=batch_num,
                    total_batches=total_batches,
                    batch_size=len(batch),
                    text_chars_total=batch_text_chars,
                    success=batch_succeeded,
                    result_count=safe_len(batch_results),
                    duration_ms=round(batch_duration * 1000, 2),
                    consecutive_failures=consecutive_failures,
                )

                # Ensure we have results for all blocks in this batch (fault-tolerant padding)
                expected_count = len(batch)
                actual_count = len(batch_results)

                if actual_count < expected_count:
                    # Pad with default results for missing blocks
                    default_result = {
                        "block_category": "Narrative",
                        "kg_node_type": None,
                        "entities": [],
                        "relationships": []
                    }
                    padding_count = expected_count - actual_count
                    padding = [default_result.copy() for _ in range(padding_count)]
                    batch_results.extend(padding)
                    total_batch_padding += padding_count

                    self.logger.warning(
                        f"Padded batch {batch_num} with {padding_count} default IE results",
                        expected_count=expected_count,
                        actual_count=actual_count,
                        padding_count=padding_count,
                        total_padding=total_batch_padding
                    )

                all_results.extend(batch_results)
                pbar.update(1)

                # Update UI progress within the "Information Extraction" phase.
                # Map text-IE batches into 35%..50% to avoid appearing "stuck".
                if progress_cb:
                    try:
                        done_batches = batch_num
                        pct = 35 + int((done_batches / max(1, total_batches)) * 15)
                        batch_display = batch_num + 1  # 1-based for user display
                        detailed_phase = f"LLM IE (Text): Batch {batch_display}/{total_batches}"
                        progress_cb(detailed_phase, pct)
                    except Exception:
                        pass
                
                # Rate limit protection: delay between batches (configurable for cloud APIs)
                batch_delay = config.get("ollama", {}).get("text", {}).get("batch_delay_seconds", 8.0)
                if batch_delay > 0 and batch_num < total_batches:
                    print(f"[IE] Rate limit protection: waiting {batch_delay}s before next batch...", flush=True)
                    time.sleep(batch_delay)
                
                # Show estimated time remaining
                if batch_num < total_batches:
                    elapsed_total = time.time() - overall_start_time
                    avg_time_per_batch = elapsed_total / batch_num
                    remaining_batches = total_batches - batch_num
                    estimated_remaining = avg_time_per_batch * remaining_batches
                    pbar.set_postfix({"est_remaining": f"{estimated_remaining:.1f}s"})
        
        # Ensure we have one result per input
        if len(all_results) != len(text_ie_inputs):
            self.logger.warning(
                "IE batch result count mismatch",
                expected=len(text_ie_inputs),
                got=len(all_results)
            )
            # Pad if needed
            if len(all_results) < len(text_ie_inputs):
                default_result = {
                    "block_category": "Narrative",
                    "kg_node_type": None,
                    "entities": [],
                    "relationships": []
                }
                all_results.extend([default_result] * (len(text_ie_inputs) - len(all_results)))
            else:
                all_results = all_results[:len(text_ie_inputs)]
        
        if batches_skipped:
            print(
                f"[IE] WARNING: {batches_skipped}/{total_batches} batches skipped due to LLM errors "
                f"({batches_skipped * batch_size} blocks filled with defaults)",
                flush=True,
            )
            self.logger.warning(
                "Text IE batches skipped due to errors",
                batches_skipped=batches_skipped,
                total_batches=total_batches,
                blocks_defaulted=batches_skipped * batch_size,
            )
        
        # Return results and total batch padding count
        return all_results, total_batch_padding

    # ------------------------------------------------------------------
    # Process evidence gate – deterministic post-IE filter
    # ------------------------------------------------------------------

    def _load_process_evidence_gate(self) -> dict[str, Any] | None:
        """Load evidence_gate config from ontology for the Process node type."""
        for nt in self._run_context.ontology.domain_definition.get("node_types", []):
            if nt.get("name") == "Process":
                gate = nt.get("evidence_gate")
                if gate and gate.get("enabled"):
                    return gate
        return None

    def _filter_unsubstantiated_processes(
        self,
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        source_text: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """Remove Process entities whose source block lacks concrete evidence.

        Scans *source_text* for four indicator categories (steps, actors,
        systems, action verbs).  Each category that has at least one hit
        scores 1 point.  If the total score < *min_evidence_score*, every
        Process entity from this block is dropped, along with any
        relationship that references a dropped entity.

        Returns (filtered_entities, filtered_relationships, dropped_count).
        """
        gate = self._load_process_evidence_gate()
        if not gate:
            return entities, relationships, 0

        process_entities = [e for e in entities if e.get("type") == "Process"]
        if not process_entities:
            return entities, relationships, 0

        min_score = gate.get("min_evidence_score", 2)
        text_lower = source_text.lower()

        score = 0
        for category in ("step_keywords", "actor_keywords", "system_keywords", "action_keywords"):
            keywords = gate.get(category, [])
            if any(kw.lower() in text_lower for kw in keywords):
                score += 1

        if score >= min_score:
            return entities, relationships, 0

        # Evidence insufficient — drop Process entities from this block
        dropped_values: set[str] = set()
        kept_entities: list[dict[str, Any]] = []
        for e in entities:
            if e.get("type") == "Process":
                dropped_values.add(e.get("value", ""))
            else:
                kept_entities.append(e)

        # Drop relationships referencing removed Process entities
        kept_rels: list[dict[str, Any]] = []
        for r in relationships:
            from_val = r.get("from_value", "")
            to_val = r.get("to_value", "")
            from_is_process = r.get("from_type") == "Process" and from_val in dropped_values
            to_is_process = r.get("to_type") == "Process" and to_val in dropped_values
            if not from_is_process and not to_is_process:
                kept_rels.append(r)

        return kept_entities, kept_rels, len(dropped_values)

    def _infer_relationships(
        self,
        entities: list[dict[str, Any]],
        existing_relationships: list[dict[str, Any]],
        domain: str
    ) -> list[dict[str, Any]]:
        """
        Infer additional relationships using ontology-based rules.

        SAFE ENHANCEMENT: Only adds relationships with confidence scores < 1.0
        Never removes existing relationships.

        Args:
            entities: List of extracted entities
            existing_relationships: List of relationships already extracted
            domain: Domain context for domain-specific rules

        Returns:
            List of inferred relationships to add
        """
        inferred = []
        existing_rel_set = {(r.get("rel_type"), r.get("from_value"), r.get("to_value"))
                           for r in existing_relationships}

        if self._run_context is None:
            raise RuntimeError("IEClient run context has not been configured")
        ontology = self._run_context.ontology.domain_definition
        relationship_defs = {
            str(rt.get("name")): rt
            for rt in ontology.get("relationship_types", []) or []
            if isinstance(rt, dict) and rt.get("name")
        }

        allowed_relationship_triples = {
            (from_type, rel_type, to_type)
            for from_type, rel_type, to_type in (
                (
                    str(from_type).strip(),
                    str(rel_type).strip(),
                    str(to_type).strip(),
                )
                for from_type, rel_type, to_type in _compile_relationship_triples(
                    relationship_defs
                )
            )
        }

        self.logger.info(
            "Relationship inference ontology contract loaded",
            allowed_relationship_triples=len(allowed_relationship_triples),
            domain=domain,
        )

        # RULE 1: Transitive IMPACTS chains (A→B→C infers A→C)
        impacts_chains = {}
        for rel in existing_relationships:
            if rel.get("rel_type") == "IMPACTS":
                from_val = rel.get("from_value")
                to_val = rel.get("to_value")
                if from_val and to_val:
                    impacts_chains.setdefault(from_val, []).append(
                        (to_val, rel.get("from_type", ""), rel.get("to_type", ""))
                    )

        for start, intermediates in impacts_chains.items():
            for mid_val, _, mid_type in intermediates:
                if mid_val in impacts_chains:
                    for end_val, _, end_type in impacts_chains[mid_val]:
                        if start != end_val:
                            rel_key = ("IMPACTS", start, end_val)
                            if rel_key not in existing_rel_set:
                                inferred.append({
                                    "rel_type": "IMPACTS",
                                    "from_type": mid_type or "Process",
                                    "from_value": start,
                                    "to_type": end_type or "Process",
                                    "to_value": end_val,
                                    "confidence": 0.6
                                })

        # RULE 2: If Process USES System, and another Requirement IMPACTS same Process,
        # infer Requirement IMPACTS System (blast radius)
        system_entities = {e["value"] for e in entities if e.get("type") == "System"}
        process_entities = {e["value"] for e in entities if e.get("type") == "Process"}
        requirement_entities = {e["value"] for e in entities if e.get("type") == "Requirement"}

        process_to_systems = {}
        for rel in existing_relationships:
            if rel.get("rel_type") == "USES" and rel.get("to_type") == "System":
                process_to_systems.setdefault(rel["from_value"], []).append(rel["to_value"])

        for rel in existing_relationships:
            if rel.get("rel_type") == "IMPACTS" and rel.get("to_value") in process_to_systems:
                for sys_val in process_to_systems[rel["to_value"]]:
                    rel_key = ("IMPACTS", rel["from_value"], sys_val)
                    if rel_key not in existing_rel_set:
                        inferred.append({
                            "rel_type": "IMPACTS",
                            "from_type": rel.get("from_type", "Requirement"),
                            "from_value": rel["from_value"],
                            "to_type": "System",
                            "to_value": sys_val,
                            "confidence": 0.5
                        })

        # Remove duplicates, self-references, and ontology-invalid inferred triples.
        deduplicated = []
        seen = set()
        rejected_invalid_triples = 0

        for rel in inferred:
            from_val = rel.get("from_value")
            to_val = rel.get("to_value")
            triple = (
                str(rel.get("from_type") or "").strip(),
                str(rel.get("rel_type") or "").strip(),
                str(rel.get("to_type") or "").strip(),
            )

            if triple not in allowed_relationship_triples:
                rejected_invalid_triples += 1
                self.logger.info(
                    "Relationship inference rejected: invalid ontology triple",
                    from_type=triple[0],
                    rel_type=triple[1],
                    to_type=triple[2],
                    from_value=from_val,
                    to_value=to_val,
                )
                continue

            if from_val and to_val and from_val != to_val:
                rel_key = (rel.get("rel_type"), from_val, to_val)
                if rel_key not in seen:
                    seen.add(rel_key)
                    deduplicated.append(rel)

        self.logger.info(
            "Relationship inference contract filtering completed",
            inferred_before_contract=len(inferred),
            rejected_invalid_triples=rejected_invalid_triples,
            admitted_inferred_relationships=len(deduplicated),
        )

        self.logger.info(
            "Relationship inference completed",
            original_relationships=len(existing_relationships),
            inferred_relationships=len(deduplicated),
            total_relationships=len(existing_relationships) + len(deduplicated)
        )

        return deduplicated



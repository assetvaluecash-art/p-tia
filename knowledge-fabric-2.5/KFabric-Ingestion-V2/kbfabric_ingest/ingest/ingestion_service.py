"""Ingestion service orchestrator."""


class IngestionCancelRequested(Exception):
    """Internal signal raised only at a durable ingestion boundary."""

import logging
import json
import os
import shutil
import time
import requests
from datetime import datetime
from pathlib import Path
from typing import Any

from kbfabric_ingest.fabric_foundry.customer.active_customer_context import ActiveCustomerContextResolver
from kbfabric_ingest.fabric_foundry.runtime.effective_ontology_context import EffectiveDomainOntologyResolver
from kbfabric_ingest.ingest.ingestion_run_context import IngestionRunContext
from kbfabric_ingest.ingest.ingestion_metadata import document_id_for_source
from kbfabric_ingest._common.telemetry import (
    get_tracer,
    record_job_start,
    record_job_end,
    record_blocks_processed,
    record_entities_extracted,
)
from kbfabric_ingest.health_checks import check_weaviate, check_neo4j
from kbfabric_ingest.ingest.config import get_app_config, get_ontology
from kbfabric_ingest.ingest.ie_client import IEClient
from kbfabric_ingest.ingest.kg_writer import KGWriter
from kbfabric_ingest.ingest.models import BlockCategory, EnrichedBlock
from kbfabric_ingest.ingest.execution_gate import HITLMode, resolve_hitl_mode
from kbfabric_ingest.ingest.pipeline.ingestion_pipeline_steps import step_map_entities, step_build_execution_plans
from kbfabric_ingest.ingest.repository.hitl_review_repository import HITLReviewRepository
from kbfabric_ingest.ingest.parser import DocumentParser
from kbfabric_ingest.ingest.pii import PIIService
from kbfabric_ingest.ingest.semantic_grouping import group_blocks
from kbfabric_ingest.ingest.weaviate_writer import WeaviateWriter
from kbfabric_ingest.llm.client import LLMClient
from kbfabric_ingest.reports.ingestion_report import IngestionReportGenerator
from kbfabric_ingest.reports.kg_ingestion_report import KGIngestionReportGenerator
from kbfabric_ingest.reports.ie_audit import collect_metrics_from_enriched_blocks, generate_ie_audit_report
from kbfabric_ingest.reports.unified_report import UnifiedIngestionReportGenerator
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import (
    log_stage_start,
    log_stage_end,
    log_stage_error,
    summarize_blocks,
    summarize_enriched_blocks,
)

logger = get_logger(__name__)


def _finalize_store_write_summary(
    store_summary: dict[str, dict[str, Any]], errors: list[dict[str, Any]], *, has_blocks: bool,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Return safe per-store outcomes and the aggregate stage state."""
    summary = {name: dict(result) for name, result in store_summary.items()}
    if not has_blocks:
        if errors:
            # An upstream stage failed before publishing started. Preserve the
            # store stage as untouched so a safe checkpoint resume can reach it.
            for result in summary.values():
                result.update(status="PENDING", reason="upstream_stage_failed")
            return summary, "PENDING"
        for result in summary.values():
            result.update(status="SKIPPED", reason="no_enriched_blocks")
        return summary, "SKIPPED"
    failures = {
        "weaviate": {"weaviate", "weaviate_validation"},
        "neo4j": {"neo4j", "neo4j_validation"},
    }
    failed_steps = {item.get("step") for item in errors}
    any_failure = False
    for store, steps in failures.items():
        if failed_steps.intersection(steps):
            error_code = (
                "STORE_WRITE_VALIDATION_FAILED"
                if f"{store}_validation" in failed_steps
                else "DEPENDENCY_FAILURE"
            )
            summary[store].update(status="FAILED", error_code=error_code)
            any_failure = True
    return summary, "FAILED" if any_failure else "SUCCEEDED"


def _resolve_relationship_admission(
    relationship: dict[str, Any],
    *,
    batch_admitted_keys: set[str],
    context_resolutions: list[Any],
) -> tuple[str | None, dict[str, str], list[dict[str, Any]]]:
    """Require admitted or explicitly resolved endpoints for an evidenced edge."""
    rel_type = str(relationship.get("rel_type") or "")
    endpoint_specs = (
        ("from", "incoming", "from_type", "from_value"),
        ("to", "outgoing", "to_type", "to_value"),
    )
    persisted_entity_ids: dict[str, str] = {}
    matched_resolutions: list[dict[str, Any]] = []

    for endpoint, direction, type_key, value_key in endpoint_specs:
        node_type = str(relationship.get(type_key) or "")
        node_value = str(relationship.get(value_key) or "")
        endpoint_key = f"{node_type}:{node_value}".lower()
        if not node_type or not node_value:
            return "endpoint_not_admitted", {}, matched_resolutions
        matching = [
            resolution
            for resolution in context_resolutions
            if str(resolution.rel_type) == rel_type
            and str(resolution.direction).lower() == direction
            and str(resolution.target_type) == node_type
            and str(resolution.target_value).casefold() == node_value.casefold()
        ]
        if matching:
            decisions = [
                getattr(resolution.decision, "value", resolution.decision)
                for resolution in matching
            ]
            for resolution, decision in zip(matching, decisions):
                matched_resolutions.append({
                    "endpoint": endpoint,
                    "decision": str(decision),
                    "reason_code": resolution.reason_code,
                    "selected_entity_id": resolution.selected_entity_id,
                })
            if any(decision != "RESOLVED" for decision in decisions):
                return "context_resolution_not_resolved", {}, matched_resolutions
            selected_ids = {
                str(resolution.selected_entity_id)
                for resolution in matching
                if resolution.selected_entity_id
                and not str(resolution.selected_entity_id).startswith("batch:")
            }
            if len(selected_ids) > 1:
                return "context_resolution_not_resolved", {}, matched_resolutions
            if selected_ids:
                persisted_entity_ids[f"{endpoint}_entity_id"] = selected_ids.pop()
            elif endpoint_key not in batch_admitted_keys:
                return "endpoint_not_admitted", {}, matched_resolutions
        elif endpoint_key not in batch_admitted_keys:
            return "endpoint_not_admitted", {}, matched_resolutions

    return None, persisted_entity_ids, matched_resolutions


def check_ollama_health(config: dict) -> bool:
    """
    Check if the configured LLM backend is responsive before starting ingestion.

    Uses Model Router gateway when ``model_router.enabled`` is true; otherwise
    probes Ollama directly.

    Args:
        config: Application configuration dictionary

    Returns:
        bool: True if the LLM backend is healthy, False otherwise
    """
    from kbfabric_ingest._common.litellm_gateway_client import (
        build_gateway_client,
        is_gateway_enabled,
    )

    try:
        ollama_config = config.get("ollama", {})
        timeout_sec = ollama_config.get("healthcheck_timeout_sec", 10)

        if is_gateway_enabled(config):
            gateway_client = build_gateway_client(config, timeout_sec=timeout_sec)
            if gateway_client and gateway_client.health_check():
                logger.info("Model Router gateway health check passed")
                return True
            logger.error("Model Router gateway health check failed")
            return False

        base_url = ollama_config["base_url"]
        text_model = ollama_config["text_model"]

        # Test with a simple generate call
        response = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": text_model,
                "prompt": "test",
                "stream": False
            },
            timeout=timeout_sec
        )

        if response.status_code == 200:
            logger.info("Ollama health check passed")
            return True
        else:
            logger.error(f"Ollama health check failed: HTTP {response.status_code}")
            return False

    except requests.exceptions.Timeout:
        logger.error("LLM health check timed out")
        return False
    except requests.exceptions.ConnectionError:
        logger.error("LLM health check failed: Connection refused")
        return False
    except Exception as e:
        logger.error(f"LLM health check failed: {e}")
        return False


class IngestionService:
    """Orchestrator for the ingestion pipeline."""

    def _admit_kg_blocks(
        self,
        enriched_blocks: list[EnrichedBlock],
        *,
        domain: str,
        ingestion_batch_id: str | None = None,
        hitl_mode: HITLMode | str | None = None,
    ) -> tuple[list[EnrichedBlock], dict[str, int]]:
        """Run Mapping -> Context Resolution -> Gate -> Plan before Neo4j.

        Weaviate is intentionally outside this boundary. Only entities whose
        execution plans permit admission are included in the Neo4j payload.
        """

        from kbfabric_ingest.utils.ingestion_observability import trace_value_ref

        raw_test_trace = os.getenv("KBFABRIC_TEST_RAW_TRACE") == "1"

        def print_test_trace(event: str, **payload: Any) -> None:
            if raw_test_trace:
                print(
                    "[KFABRIC-RAW-TEST-TRACE] "
                    + json.dumps({"event": event, **payload}, default=str, sort_keys=True),
                    flush=True,
                )

        run_context = self._run_context
        trace_scope = {
            "ingestion_batch_id": ingestion_batch_id,
            "customer_id": run_context.tenant_context.customer_id if run_context else None,
            "tenant_id": run_context.tenant_context.tenant_id if run_context else None,
            "industry_id": run_context.tenant_context.industry_id if run_context else None,
            "domain_id": run_context.ontology.domain_id if run_context else domain,
            "product_line_id": run_context.ontology.product_line_id if run_context else None,
            "customer_ontology_id": str(run_context.ontology.customer_ontology_id) if run_context else None,
            "customer_ontology_version": run_context.ontology.customer_ontology_version if run_context else None,
            "canonical_ontology_version": run_context.ontology.ontology_version if run_context else None,
            "industry_extraction_policy_version": (
                str((run_context.ontology.extraction_policy or {}).get("version"))
                if run_context else None
            ),
        }
        admission_started = log_stage_start(
            self.logger, "kg_admission", domain=domain,
            enriched_block_count=len(enriched_blocks), **trace_scope,
        )

        deps = self.ie_client._get_pipeline_deps()
        hitl_mode = resolve_hitl_mode(
            hitl_mode or (run_context.hitl_mode if run_context else None)
        )
        hitl_repo = HITLReviewRepository()

        admitted_blocks: list[EnrichedBlock] = []
        stats = {
            "blocks_seen": 0,
            "blocks_kg_eligible": 0,
            "entities_mapped": 0,
            "write_now": 0,
            "write_unresolved": 0,
            "hitl_queued": 0,
            "rejected": 0,
            "entities_admitted": 0,
            "relationships_admitted": 0,
            "relationships_endpoint_resolution_recorded": 0,
            "relationships_resolved": 0,
            "relationships_unresolved": 0,
            "relationships_withheld": 0,
            "hitl_review_ids": [],
        }

        from hashlib import sha256
        from kbfabric_ingest.ingest.lane2_node_reconciliation import ExistingNode

        batch_candidates_by_id: dict[str, ExistingNode] = {}
        for candidate_block in enriched_blocks:
            if not candidate_block or not candidate_block.block or not candidate_block.kg_node_type:
                continue
            for candidate_entity in candidate_block.candidate_entities or []:
                candidate_name = str(candidate_entity.get("value") or "").strip()
                candidate_type = str(candidate_entity.get("type") or candidate_block.kg_node_type).strip()
                if not candidate_name or not candidate_type:
                    continue
                identity = f"{domain}:{candidate_type}:{candidate_name.casefold()}"
                candidate_id = "batch:" + sha256(identity.encode()).hexdigest()[:24]
                batch_candidates_by_id[candidate_id] = ExistingNode(
                    entity_id=candidate_id,
                    name=candidate_name,
                    name_display=candidate_name,
                    aliases=[str(alias) for alias in candidate_entity.get("aliases", []) if isinstance(alias, str)],
                    kg_node_type=candidate_type,
                    labels=[candidate_type],
                    description=str(candidate_entity.get("description") or "") or None,
                    raw={"candidate_source": "current_ingestion_batch", "domain": domain},
                )
        batch_candidates = list(batch_candidates_by_id.values())

        prepared_decisions: dict[int, tuple[list[Any], list[Any]]] = {}
        batch_admitted_keys: set[str] = set()
        for candidate_block in enriched_blocks:
            if not candidate_block or not candidate_block.block or not candidate_block.kg_node_type or candidate_block.kg_node_type.startswith("_"):
                continue
            candidate_entities = list(candidate_block.candidate_entities or [])
            candidate_relationships = list(candidate_block.candidate_relationships or [])
            if not candidate_entities:
                continue
            candidate_provenance = {
                "product_line_id": self._run_context.ontology.product_line_id,
                "block_category": (candidate_block.block_category.value if hasattr(candidate_block.block_category, "value") else str(candidate_block.block_category)),
                "kg_node_type": candidate_block.kg_node_type,
                "spine_node": candidate_block.spine_node,
            }
            map_started = log_stage_start(
                self.logger, "entity_mapping", **trace_scope,
                block_id=candidate_block.block.block_id,
                entity_count=len(candidate_entities),
                relationship_count=len(candidate_relationships),
            )
            _, candidate_mappings = step_map_entities(
                candidate_entities, candidate_relationships,
                block=candidate_block.block, lane="kg_admission", deps=deps,
                provenance=candidate_provenance, batch_candidates=batch_candidates,
            )
            log_stage_end(
                self.logger, "entity_mapping", map_started, **trace_scope,
                block_id=candidate_block.block.block_id,
                entity_count=len(candidate_entities),
                mapping_count=len(candidate_mappings),
                context_resolution_count=sum(len(m.context_resolutions or []) for m in candidate_mappings),
                relationship_count=len(candidate_relationships),
            )
            if not candidate_mappings:
                continue
            plan_started = log_stage_start(
                self.logger, "execution_planning", **trace_scope,
                block_id=candidate_block.block.block_id,
                entity_count=len(candidate_mappings), hitl_mode=hitl_mode.value,
            )
            candidate_plans = step_build_execution_plans(
                candidate_mappings, hitl_mode=hitl_mode,
                ingestion_batch_id=ingestion_batch_id,
                ontology_version=self._run_context.ontology.ontology_version if self._run_context else None,
            )
            log_stage_end(
                self.logger, "execution_planning", plan_started, **trace_scope,
                block_id=candidate_block.block.block_id,
                entity_count=len(candidate_mappings),
                write_now=sum(1 for plan in candidate_plans if plan.action.value == "WRITE_NOW"),
                write_unresolved=sum(1 for plan in candidate_plans if plan.action.value == "WRITE_UNRESOLVED"),
                queue_for_review=sum(1 for plan in candidate_plans if plan.action.value == "QUEUE_FOR_REVIEW"),
                reject=sum(1 for plan in candidate_plans if plan.action.value == "REJECT"),
            )
            prepared_decisions[id(candidate_block)] = (candidate_mappings, candidate_plans)
            for mapping, plan in zip(candidate_mappings, candidate_plans):
                if plan.action.value in {"WRITE_NOW", "WRITE_UNRESOLVED"}:
                    batch_admitted_keys.add(f"{mapping.incoming.node_type}:{mapping.incoming.name}".lower())

        for enriched in enriched_blocks:
            stats["blocks_seen"] += 1

            if not enriched or not enriched.block:
                continue

            if not enriched.kg_node_type or enriched.kg_node_type.startswith("_"):
                self.logger.info(
                    "ingestion.kg_block.skipped", **trace_scope,
                    block_id=enriched.block.block_id,
                    block_category=(enriched.block_category.value if hasattr(enriched.block_category, "value") else str(enriched.block_category)),
                    kg_node_type=enriched.kg_node_type, reason_code="no_kg_node_type",
                )
                continue

            entities = list(enriched.candidate_entities or [])
            relationships = list(enriched.candidate_relationships or [])

            print_test_trace(
                "block_candidates",
                ingestion_batch_id=ingestion_batch_id,
                block_id=enriched.block.block_id,
                source_file=enriched.block.source_file,
                entity_type=enriched.kg_node_type,
                entities=entities,
                relationships=relationships,
            )

            if not entities:
                self.logger.info(
                    "ingestion.kg_block.skipped", **trace_scope,
                    block_id=enriched.block.block_id,
                    block_category=(enriched.block_category.value if hasattr(enriched.block_category, "value") else str(enriched.block_category)),
                    kg_node_type=enriched.kg_node_type, reason_code="no_candidate_entities",
                    candidate_relationship_count=len(relationships),
                )
                continue

            stats["blocks_kg_eligible"] += 1

            mappings, plans = prepared_decisions.get(id(enriched), ([], []))
            stats["entities_mapped"] += len(mappings)
            if not mappings:
                continue

            # Mapping results and plans are intentionally positional.
            admitted_keys: set[str] = set()
            withheld_keys: set[str] = set()
            mapping_by_key: dict[str, Any] = {}

            for mapping, plan in zip(mappings, plans):
                key = (
                    f"{mapping.incoming.node_type}:"
                    f"{mapping.incoming.name}"
                ).lower()
                mapping_by_key[key] = (mapping, plan)
                self.logger.info(
                    "ingestion.entity.admission.outcome",
                    **trace_scope, block_id=enriched.block.block_id,
                    entity_type=mapping.incoming.node_type,
                    entity_ref=trace_value_ref(mapping.incoming.name),
                    mapping_decision=mapping.decision.value,
                    mapping_reason=mapping.reason_code,
                    plan_action=plan.action.value,
                    plan_reason=plan.mapping_decision,
                    context_resolution_count=len(mapping.context_resolutions or []),
                )
                print_test_trace(
                    "entity_admission_decision",
                    ingestion_batch_id=ingestion_batch_id,
                    block_id=enriched.block.block_id,
                    entity_type=mapping.incoming.node_type,
                    entity_name=mapping.incoming.name,
                    mapping_decision=mapping.decision.value,
                    mapping_reason=mapping.reason_code,
                    selected_entity_id=mapping.selected_entity_id,
                    plan_action=plan.action.value,
                    plan_reason=plan.justification or plan.mapping_decision,
                    context_resolutions=[
                        resolution.model_dump(mode="json")
                        for resolution in (mapping.context_resolutions or [])
                    ],
                )

                if plan.action.value in {"WRITE_NOW", "WRITE_UNRESOLVED"}:
                    admitted_keys.add(key)

                    if plan.action.value == "WRITE_NOW":
                        stats["write_now"] += 1
                    else:
                        stats["write_unresolved"] += 1
                elif plan.action.value == "QUEUE_FOR_REVIEW":
                    withheld_keys.add(key)
                    stats["hitl_queued"] += 1

                    reason = str(plan.justification or plan.mapping_decision or "review_required")
                    has_relationship_review = any(
                        str(resolution.get("decision", "")).upper()
                        in {"RELATIONSHIP_REVIEW_REQUIRED", "UNRESOLVED_REFERENCE"}
                        for resolution in (plan.context_resolutions or [])
                    )
                    queue_type = (
                        "CONTEXT_RESOLUTION"
                        if has_relationship_review and mapping.decision.value != "REQUIRES_REVIEW"
                        else "NODE_ADMISSION"
                    )
                    admission_behavior = (
                        "NODE_CAN_EXIST"
                        if queue_type == "CONTEXT_RESOLUTION"
                        else "WAIT_FOR_NODE_DECISION"
                    )

                    review_id = hitl_repo.enqueue(
                        plan=plan,
                        ingestion_batch_id=ingestion_batch_id,
                        queue_type=queue_type,
                        reason_code=plan.justification or "REVIEW_REQUIRED",
                        admission_behavior=admission_behavior,
                        short_summary=(
                            f"KG admission review for "
                            f"{mapping.incoming.node_type}: {mapping.incoming.name}"
                        ),
                        decision_question=(
                            "Should this extracted entity/relationship be admitted to the knowledge graph?"
                        ),
                        source_context={
                            "block_id": enriched.block.block_id,
                            "source_block_ids": enriched.block.source_block_ids or [enriched.block.block_id],
                            "source_file": enriched.block.source_file,
                            "page_number": enriched.block.page_number,
                            "block_index": enriched.block.block_index,
                            "block_type": enriched.block.block_type,
                            # HITL evidence must use the PII-masked representation.
                            "masked_text": enriched.masked_text,
                            "semantic_unit_id": enriched.block.semantic_unit_id,
                        },
                        document_context={
                            "document_id": (
                                document_id_for_source(
                                    self._run_context.tenant_context.customer_id,
                                    enriched.block.source_file,
                                ) if self._run_context else None
                            ),
                            "source_file": enriched.block.source_file,
                            "domain_id": domain,
                            "product_line_id": (
                                self._run_context.ontology.product_line_id
                                if self._run_context else None
                            ),
                            "product_line_name": (
                                self._run_context.ontology.product_line_name
                                if self._run_context else None
                            ),
                            "project_ids": (enriched.block.metadata or {}).get("project_ids", []),
                            "context_tags": (enriched.block.metadata or {}).get("context_tags", []),
                        },
                        ontology_context={
                            "customer_id": trace_scope["customer_id"],
                            "tenant_id": trace_scope["tenant_id"],
                            "industry_id": trace_scope["industry_id"],
                            "domain_id": trace_scope["domain_id"],
                            "product_line_id": trace_scope["product_line_id"],
                            "customer_ontology_id": trace_scope["customer_ontology_id"],
                            "customer_ontology_version": trace_scope["customer_ontology_version"],
                            "canonical_ontology_version": trace_scope["canonical_ontology_version"],
                            "industry_extraction_policy_version": trace_scope["industry_extraction_policy_version"],
                            "kg_node_type": enriched.kg_node_type,
                            "spine_node": enriched.spine_node,
                        },
                        mapping_context={
                            "incoming_entity": mapping.incoming.model_dump(mode="json"),
                            "decision": mapping.decision.value,
                            "reason_code": mapping.reason_code,
                            "selected_entity_id": mapping.selected_entity_id,
                            "confidence": mapping.score_breakdown.combined_confidence,
                            "score_breakdown": mapping.score_breakdown.model_dump(mode="json"),
                            "candidate_scores": [
                                candidate.model_dump(mode="json")
                                for candidate in mapping.candidates
                            ],
                            "ontology_constraints": mapping.ontology_constraints,
                            "proposed_incoming": [item.model_dump(mode="json") for item in mapping.proposed_incoming],
                            "proposed_outgoing": [item.model_dump(mode="json") for item in mapping.proposed_outgoing],
                            "provenance": mapping.provenance,
                        },
                        review_payload={
                            "hitl_mode": hitl_mode.value,
                            "decision_type": queue_type,
                            "run_scope": trace_scope,
                            "rhfl": {
                                "status": "NOT_COMPUTED",
                                "query_id": None,
                                "retrieved_passage_ids": [],
                                "retrieval_scores": [],
                                "reranker_scores": [],
                            },
                        },
                        relationship_evidence=[
                            r.model_dump(mode="json")
                            for r in (mapping.context_resolutions or [])
                        ],
                    )
                    stats["hitl_review_ids"].append(review_id)
                    print_test_trace(
                        "hitl_review_queued",
                        ingestion_batch_id=ingestion_batch_id,
                        review_id=review_id,
                        queue_type=queue_type,
                        status="PENDING",
                        entity_type=mapping.incoming.node_type,
                        entity_name=mapping.incoming.name,
                        plan_action=plan.action.value,
                        reason=plan.justification or plan.mapping_decision,
                        relationship_evidence=[
                            resolution.model_dump(mode="json")
                            for resolution in (mapping.context_resolutions or [])
                        ],
                    )
                    self.logger.info(
                        "ingestion.hitl.item.queued",
                        **trace_scope, block_id=enriched.block.block_id,
                        entity_type=mapping.incoming.node_type,
                        entity_ref=trace_value_ref(mapping.incoming.name),
                        queue_type=queue_type,
                        reason_ref=trace_value_ref(plan.justification or "REVIEW_REQUIRED"),
                        admission_behavior=admission_behavior,
                    )
                else:
                    withheld_keys.add(key)
                    stats["rejected"] += 1

            if not admitted_keys:
                continue

            # Keep only admitted entities.
            admitted_entities = []
            for entity in entities:
                key = f"{entity.get('type')}:{entity.get('value')}".lower()
                if key in admitted_keys:
                    admitted_entities.append(entity)

            # Both endpoints must be admitted from this batch or resolved to
            # persisted Neo4j identities; unresolved/review-required endpoints
            # remain withheld.
            admitted_relationships = []
            for rel in relationships:
                eligible_resolutions = [
                    resolution
                    for mapping, plan in zip(mappings, plans)
                    if plan.action.value in {"WRITE_NOW", "WRITE_UNRESOLVED"}
                    for resolution in (mapping.context_resolutions or [])
                ]
                outcome_reason, resolved_endpoint_ids, endpoint_resolutions = (
                    _resolve_relationship_admission(
                        rel,
                        batch_admitted_keys=batch_admitted_keys,
                        context_resolutions=eligible_resolutions,
                    )
                )
                resolution_decision = (
                    ";".join(item["decision"] for item in endpoint_resolutions)
                    if endpoint_resolutions else "NOT_RECORDED"
                )
                resolution_reason_code = ";".join(
                    str(item["reason_code"] or "") for item in endpoint_resolutions
                ) or None
                outcome = "WITHHELD" if outcome_reason else "ADMITTED"
                self.logger.info(
                    "ingestion.relationship.admission.outcome",
                    **trace_scope, block_id=enriched.block.block_id,
                    relationship_type=rel.get("rel_type"),
                    source_type=rel.get("from_type"),
                    source_ref=trace_value_ref(rel.get("from_value")),
                    target_type=rel.get("to_type"),
                    target_ref=trace_value_ref(rel.get("to_value")),
                    resolution_decision=resolution_decision,
                    resolution_reason_code=resolution_reason_code,
                    endpoint_resolutions=endpoint_resolutions,
                    resolved_endpoint_ids=resolved_endpoint_ids,
                    outcome=outcome, reason_code=outcome_reason,
                )
                if resolution_decision != "NOT_RECORDED":
                    stats["relationships_endpoint_resolution_recorded"] += 1
                if outcome_reason is None:
                    admitted_relationships.append({**rel, **resolved_endpoint_ids})
                    if endpoint_resolutions:
                        stats["relationships_resolved"] += 1
                else:
                    stats["relationships_withheld"] += 1
                    if any(
                        item["decision"] != "RESOLVED"
                        for item in endpoint_resolutions
                    ):
                        stats["relationships_unresolved"] += 1
                print_test_trace(
                    "relationship_admission_decision",
                    ingestion_batch_id=ingestion_batch_id,
                    block_id=enriched.block.block_id,
                    relationship=rel,
                    endpoint_resolution=resolution_decision,
                    endpoint_resolution_reason=resolution_reason_code,
                    resolved_endpoint_ids=resolved_endpoint_ids,
                    endpoint_resolutions=endpoint_resolutions,
                    outcome=outcome,
                    reason=outcome_reason,
                )

            if not admitted_entities:
                continue

            admitted = enriched.model_copy(
                deep=True,
                update={
                    "candidate_entities": admitted_entities,
                    "candidate_relationships": admitted_relationships,
                },
            )

            self.logger.info(
                "kg_admission.block_admitted",
                ingestion_batch_id=ingestion_batch_id,
                block_id=enriched.block.block_id,
                admitted_entity_count=len(admitted_entities),
                admitted_relationship_count=len(admitted_relationships),
            )

            admitted_blocks.append(admitted)
            stats["entities_admitted"] += len(admitted_entities)
            stats["relationships_admitted"] += len(admitted_relationships)

        log_stage_end(
            self.logger, "kg_admission", admission_started,
            domain=domain, hitl_mode=hitl_mode.value,
            admitted_block_count=len(admitted_blocks), **trace_scope, **stats,
        )
        self.logger.info(
            "kg_admission.completed",
            domain=domain, hitl_mode=hitl_mode.value,
            admitted_block_count=len(admitted_blocks), **trace_scope, **stats,
        )
        print_test_trace(
            "kg_admission_summary",
            ingestion_batch_id=ingestion_batch_id,
            hitl_mode=hitl_mode.value,
            **stats,
        )

        return admitted_blocks, stats

    def __init__(self):
        """Initialize ingestion service with all components."""
        print("[IngestionService] Starting initialization...", flush=True)
        self.config = get_app_config()
        print("[IngestionService] Config loaded", flush=True)
        print("[IngestionService] Ontology loaded", flush=True)
        self.logger = get_logger(__name__)

        # Run-scoped authoritative context is resolved at ingestion start.
        # Customer/tenant are backend-owned; the UI never supplies them.
        self.customer_context_resolver = ActiveCustomerContextResolver()
        self.ontology_context_resolver = EffectiveDomainOntologyResolver()

        self._run_context: IngestionRunContext | None = None
        
        # Initialize components
        max_chars = self.config["ingestion"]["max_chars_per_block"]
        print(f"[IngestionService] Creating DocumentParser (max_chars={max_chars})...", flush=True)
        # Note: artifacts_dir will be set in run_ingestion when job_id is available
        self.parser = DocumentParser(max_chars_per_block=max_chars, artifacts_dir=None)
        print("[IngestionService] DocumentParser created", flush=True)
        
        print("[IngestionService] Creating PIIService...", flush=True)
        self.pii_service = PIIService.from_config()
        print("[IngestionService] PIIService created", flush=True)
        
        print("[IngestionService] Creating LLMClient...", flush=True)
        # LLMClient creates EmbeddingsClient singleton - don't load model here
        self.llm_client = LLMClient(self.config.get("ollama", {}))
        print("[IngestionService] LLMClient created", flush=True)
        
        print("[IngestionService] Creating IEClient...", flush=True)
        self.ie_client = IEClient(self.pii_service, self.llm_client)
        print("[IngestionService] IEClient created", flush=True)
        
        print("[IngestionService] Creating WeaviateWriter...", flush=True)
        self.weaviate_writer = WeaviateWriter(self.config.get("weaviate", {}))
        print("[IngestionService] WeaviateWriter created", flush=True)
        
        print("[IngestionService] Creating KGWriter...", flush=True)
        self.kg_writer = KGWriter(self.config.get("neo4j", {}), self.llm_client)
        print("[IngestionService] KGWriter created", flush=True)
        
        print("[IngestionService] Creating IngestionReportGenerator...", flush=True)
        self.report_generator = IngestionReportGenerator()
        print("[IngestionService] IngestionReportGenerator created", flush=True)
        
        print("[IngestionService] Creating KGIngestionReportGenerator...", flush=True)
        self.kg_report_generator = KGIngestionReportGenerator()
        print("[IngestionService] KGIngestionReportGenerator created", flush=True)

        print("[IngestionService] Creating UnifiedIngestionReportGenerator...", flush=True)
        self.unified_report_generator = UnifiedIngestionReportGenerator()
        print("[IngestionService] UnifiedIngestionReportGenerator created", flush=True)

        # NOTE: EMF/WMF conversion intentionally removed. Unsupported images are skipped with audit entries.

        # Ensure schemas (with error handling to avoid blocking initialization)
        # These operations connect to external services and could hang if services are down
        print("[IngestionService] Ensuring Weaviate schema...", flush=True)
        try:
            self.logger.info("Ensuring Weaviate schema...")
            self.weaviate_writer.ensure_schema()
            self.logger.info("Weaviate schema ensured")
            print("[IngestionService] Weaviate schema ensured", flush=True)
        except Exception as e:
            self.logger.warning(f"Failed to ensure Weaviate schema (will retry later): {e}")
            print(f"[IngestionService] WARNING: Failed to ensure Weaviate schema: {e}", flush=True)
        
        print("[IngestionService] Ensuring Neo4j indexes...", flush=True)
        try:
            self.logger.info("Ensuring Neo4j indexes...")
            self.kg_writer.ensure_indexes()
            self.logger.info("Neo4j indexes ensured")
            print("[IngestionService] Neo4j indexes ensured", flush=True)
        except Exception as e:
            self.logger.warning(f"Failed to ensure Neo4j indexes (will retry later): {e}")
            print(f"[IngestionService] WARNING: Failed to ensure Neo4j indexes: {e}", flush=True)

        # Validate schemas/indexes (non-blocking; used for report visibility)
        self.weaviate_schema_validation = {}
        self.neo4j_index_validation = {}
        try:
            self.weaviate_schema_validation = self.weaviate_writer.validate_schema()
        except Exception as e:
            self.logger.warning("Weaviate schema validation failed", error=str(e))
            self.weaviate_schema_validation = {"status": "error", "error": str(e)}
        try:
            self.neo4j_index_validation = self.kg_writer.validate_indexes()
        except Exception as e:
            self.logger.warning("Neo4j index validation failed", error=str(e))
            self.neo4j_index_validation = {"status": "error", "error": str(e)}
        
        print("[IngestionService] Initialization complete", flush=True)

    def close(self) -> None:
        """Close all connections and resources."""
        try:
            if hasattr(self, 'kg_writer') and self.kg_writer:
                self.kg_writer.close()
                print("[IngestionService] Neo4j connection closed", flush=True)
        except Exception as e:
            print(f"[IngestionService] Warning: Failed to close Neo4j connection: {e}", flush=True)

        try:
            if hasattr(self, 'weaviate_writer') and self.weaviate_writer:
                self.weaviate_writer.close()
                print("[IngestionService] Weaviate connection cleanup complete", flush=True)
        except Exception as e:
            print(f"[IngestionService] Warning: Failed to cleanup Weaviate connection: {e}", flush=True)

    # _check_emf_conversion_tools() removed

    def clear_domain(self, domain: str) -> dict[str, Any]:
        """
        Clear all existing data for a domain from Weaviate and Neo4j.
        
        Uses the established, authenticated connections.
        
        Args:
            domain: Domain name (e.g., "wealth_portfolio")
            
        Returns:
            dict with results from both Weaviate and Neo4j clearing operations
        """
        print(f"[IngestionService] Clearing domain '{domain}'...", flush=True)
        self.logger.info("Clearing domain data", domain=domain)
        
        results = {
            "domain": domain,
            "weaviate": None,
            "neo4j": None
        }
        
        # Clear Weaviate using existing writer
        try:
            results["weaviate"] = self.weaviate_writer.clear_domain(domain)
            print(f"[IngestionService] Weaviate clear: {results['weaviate']}", flush=True)
        except Exception as e:
            results["weaviate"] = {"status": "error", "error": str(e)}
            print(f"[IngestionService] Weaviate clear failed: {e}", flush=True)
        
        # Clear Neo4j using existing writer  
        try:
            results["neo4j"] = self.kg_writer.clear_domain(domain)
            print(f"[IngestionService] Neo4j clear: {results['neo4j']}", flush=True)
        except Exception as e:
            results["neo4j"] = {"status": "error", "error": str(e)}
            print(f"[IngestionService] Neo4j clear failed: {e}", flush=True)
        
        self.logger.info("Domain cleared", domain=domain, results=results)
        print(f"[IngestionService] Domain '{domain}' clearing complete", flush=True)
        
        return results

    def clear_product_line(
        self,
        *,
        customer_id: str,
        tenant_id: str,
        domain_id: str,
        product_line_id: str,
    ) -> dict[str, Any]:
        """Clear one customer/domain/product-line data scope without deleting ontology context."""
        weaviate_result = self.weaviate_writer.clear_product_line(
            customer_id=customer_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            product_line_id=product_line_id,
        )
        neo4j_result = self.kg_writer.clear_product_line(
            customer_id=customer_id,
            tenant_id=tenant_id,
            domain_id=domain_id,
            product_line_id=product_line_id,
        )
        if weaviate_result.get("status") != "cleared":
            raise RuntimeError(f"Weaviate product-line cleanup failed: {weaviate_result}")
        if neo4j_result.get("status") != "cleared":
            raise RuntimeError(f"Neo4j product-line cleanup failed: {neo4j_result}")
        result = {
            "customer_id": customer_id,
            "tenant_id": tenant_id,
            "domain_id": domain_id,
            "product_line_id": product_line_id,
            "weaviate": weaviate_result,
            "neo4j": neo4j_result,
            "customer_ontology_preserved": True,
        }
        self.logger.info("Product-line scope cleared", **result)
        return result

    def run_ingestion(
        self,
        input_dir: Path,
        domain: str,
        progress_tracker=None,
        skip_text_ie: bool = False,
        product_line_id: str | None = None,
        product_line_name: str | None = None,
        job_id: str | None = None,
        job_output_log_path: str | None = None,
        project_ids: str | None = None,
        context_tags: str | None = None,
        hitl_mode: HITLMode | str | None = None,
        document_nature: str = "qe_assets",
        customer_id: str | None = None,
        customer_ontology_version: int | None = None,
    ) -> dict[str, Any]:
        """
        Run the complete ingestion pipeline with comprehensive error handling.
        
        Args:
            input_dir: Input directory containing files to ingest
            domain: Domain name (e.g., "trade_finance", "wealth_portfolio", "consumer_payments")
            progress_tracker: Optional progress tracker instance for Ray Jobs
            
        Returns:
            Dictionary with ingestion summary and report paths, or error details if ingestion fails
        """
        # Derive job id from progress tracker if not provided
        if job_id is None and progress_tracker is not None:
            job_id = getattr(progress_tracker, "job_id", None)
        # Setup job-specific file logging for persistence
        job_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logs_dir = Path("logs/ingestion_jobs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        job_log_file = logs_dir / f"ingestion_{domain}_{job_timestamp}.log"
        if job_id:
            report_basename = job_id if job_id.startswith("ingest-") else f"ingest-{job_id}"
        else:
            report_basename = f"ingestion_{domain}_{job_timestamp}"

        ingestion_batch_id = str(job_id or report_basename)

        # Setup artifacts directory for PDFs and uplifted images
        artifacts_dir = Path("artifacts") / report_basename
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.parser.artifacts_dir = artifacts_dir
        self.parser.pdfs_dir = artifacts_dir / "pdfs"
        self.parser.images_dir = artifacts_dir / "images"
        self.parser.pdfs_dir.mkdir(parents=True, exist_ok=True)
        self.parser.images_dir.mkdir(parents=True, exist_ok=True)
        print(f"[IngestionService] Artifacts will be saved to: {artifacts_dir}", flush=True)
        
        # Ensure LLM raw I/O logs use the SAME run timestamp as reports
        try:
            self.llm_client.set_run_id(job_timestamp)
        except Exception:
            # Logging should never break ingestion
            pass
        
        # Create file handler for this job
        file_handler = logging.FileHandler(job_log_file, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        
        # Add handler to root logger (structlog loggers write through root logger)
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)
        
        self.logger.info(
            "="*80 + "\n" +
            f"INGESTION JOB STARTED: {job_timestamp}\n" +
            f"Domain: {domain}\n" +
            f"Input Directory: {input_dir}\n" +
            f"Log File: {job_log_file}\n" +
            "="*80
        )
        
        # Start telemetry
        tracer = get_tracer()
        job_start_time = time.time()
        record_job_start(domain)

        # Pre-flight dependency health checks (Ollama/Weaviate/Neo4j)
        preflight_started = log_stage_start(self.logger, "preflight", job_id=job_id, domain=domain)
        ollama_ok = True

        # Pre-flight health check for Ollama
        if not skip_text_ie:  # Only check if we're going to do IE
            print(f"[INGEST][{domain}] Running pre-flight Ollama health check...", flush=True)
            ollama_ok = check_ollama_health(self.config)
            if not ollama_ok:
                error_msg = "Ollama service is not available. Please ensure Ollama is running and accessible before starting ingestion jobs."
                self.logger.error(error_msg)
                log_stage_error(self.logger, "preflight", error_msg, job_id=job_id, domain=domain,
                                ollama_ok=False, started_at=preflight_started)
                print(f"[INGEST][{domain}] ERROR: {error_msg}", flush=True)
                raise RuntimeError(error_msg)
            print(f"[INGEST][{domain}] Ollama health check passed", flush=True)

        # Pre-flight health checks for Weaviate and Neo4j (required for every run)
        print(f"[INGEST][{domain}] Running pre-flight Weaviate health check...", flush=True)
        weaviate_ok, weaviate_err = check_weaviate(self.config)
        if not weaviate_ok:
            error_msg = f"Weaviate is not available. {weaviate_err or 'Connection failed.'}"
            self.logger.error(error_msg)
            log_stage_error(self.logger, "preflight", error_msg, job_id=job_id, domain=domain,
                            ollama_ok=ollama_ok, weaviate_ok=False, started_at=preflight_started)
            print(f"[INGEST][{domain}] ERROR: {error_msg}", flush=True)
            raise RuntimeError(error_msg)
        print(f"[INGEST][{domain}] Weaviate health check passed", flush=True)

        print(f"[INGEST][{domain}] Running pre-flight Neo4j health check...", flush=True)
        neo4j_ok, neo4j_err = check_neo4j(self.config)
        if not neo4j_ok:
            error_msg = f"Neo4j is not available. {neo4j_err or 'Connection failed.'}"
            self.logger.error(error_msg)
            log_stage_error(self.logger, "preflight", error_msg, job_id=job_id, domain=domain,
                            ollama_ok=ollama_ok, weaviate_ok=weaviate_ok, neo4j_ok=False, started_at=preflight_started)
            print(f"[INGEST][{domain}] ERROR: {error_msg}", flush=True)
            raise RuntimeError(error_msg)
        print(f"[INGEST][{domain}] Neo4j health check passed", flush=True)

        log_stage_end(
            self.logger, "preflight", preflight_started,
            job_id=job_id, domain=domain,
            ollama_ok=ollama_ok, weaviate_ok=weaviate_ok, neo4j_ok=neo4j_ok,
        )

        def update_progress(phase: str, phase_num: int, pct: int, **kwargs):
            """Helper to update progress if tracker is provided."""
            if progress_tracker:
                progress_tracker.set_phase(phase, phase_num, pct)
                if kwargs:
                    progress_tracker.set_counts(**kwargs)
        
        # Wrap entire ingestion in a span
        with tracer.start_as_current_span("ingestion.run_ingestion") as span:
            span.set_attribute("domain", domain)
            span.set_attribute("input_dir", str(input_dir))
            
            # Validate inputs
        if not isinstance(input_dir, Path):
            input_dir = Path(input_dir)
        
        if not input_dir.exists():
            error_msg = f"Input directory does not exist: {input_dir}"
            self.logger.error(error_msg)
            return {
                "input_dir": str(input_dir),
                "domain": domain,
                "error": error_msg,
                "total_files": 0,
                "total_blocks": 0,
                "blocks_per_domain": {},
                "blocks_per_category": {},
                "pii_summary": {},
                "report_paths": {}
            }
        
        if not domain:
            error_msg = "Domain parameter is required"
            self.logger.error(error_msg)
            return {
                "input_dir": str(input_dir),
                "domain": domain or "unknown",
                "error": error_msg,
                "total_files": 0,
                "total_blocks": 0,
                "blocks_per_domain": {},
                "blocks_per_category": {},
                "pii_summary": {},
                "report_paths": {}
            }
        
        tenant_context = self.customer_context_resolver.resolve(customer_id=customer_id)
        ontology_context = self.ontology_context_resolver.resolve(
            customer_id=tenant_context.customer_id,
            domain_id=domain,
            product_line_id=product_line_id,
            product_line_name=product_line_name,
            customer_ontology_version=customer_ontology_version,
        )
        effective_hitl_mode = resolve_hitl_mode(hitl_mode)
        run_context = IngestionRunContext(
            tenant_context=tenant_context,
            ontology=ontology_context,
            hitl_mode=effective_hitl_mode.value,
        )
        self._run_context = run_context
        self.ie_client.configure_run_context(run_context)
        self.kg_writer.configure_run_context(run_context)
        self.weaviate_writer.configure_run_context(run_context)

        if self._run_context is None:
            raise RuntimeError("Ingestion run context was not frozen")

        self.logger.info(
            "ingestion.run_context.frozen",
            tenant_id=self._run_context.tenant_context.tenant_id,
            customer_id=self._run_context.tenant_context.customer_id,
            domain_id=self._run_context.ontology.domain_id,
            product_line_id=self._run_context.ontology.product_line_id,
            ontology_version=self._run_context.ontology.ontology_version,
            customer_ontology_version=self._run_context.ontology.customer_ontology_version,
            hitl_mode=self._run_context.hitl_mode,
        )

        self.logger.info(
            "ingestion.run_context.frozen",
            tenant_id=tenant_context.tenant_id,
            customer_id=tenant_context.customer_id,
            domain_id=ontology_context.domain_id,
            product_line_id=ontology_context.product_line_id,
            customer_ontology_id=str(ontology_context.customer_ontology_id),
            customer_ontology_version=ontology_context.customer_ontology_version,
        )

        self.logger.info("Starting ingestion", input_dir=str(input_dir), domain=domain)
        errors = []
        grouping_stats: dict[str, Any] | None = None
        canonical_blocks = []
        enriched_blocks = []
        restored_admitted_blocks = []
        restored_admission_stats: dict[str, int] = {}
        restored_checkpoint_stage: str | None = None
        if progress_tracker and int((getattr(progress_tracker, "state", {}).get("audit_context") or {}).get("execution_attempt", 1)) > 1:
            restored = progress_tracker.restore_latest_blocks_checkpoint()
            if restored:
                restored_checkpoint_stage = restored["stage_id"]
                canonical_blocks = restored.get("canonical_blocks", [])
                enriched_blocks = restored.get("enriched_blocks", [])
                restored_admitted_blocks = restored.get("admitted_blocks", [])
                restored_admission_stats = restored.get("admission_stats", {})
                print(
                    f"[CHECKPOINT] Restored {restored_checkpoint_stage} "
                    f"({len(canonical_blocks)} canonical, {len(enriched_blocks)} enriched blocks)",
                    flush=True,
                )
        kb_ids = {}
        phase_durations: dict[str, float] = {}
        
        # Count files for progress tracking
        files = list(input_dir.glob("*.*"))
        files = [f for f in files if f.suffix.lower() in {".pdf", ".docx", ".xlsx", ".txt", ".md", ".csv", ".pptx"}]
        if progress_tracker:
            progress_tracker.set_files(0, len(files))
        
        # Step 1: Parse folder
        if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
            update_progress("Parsing Documents", 3, 20)
            print(f"[INGEST][{domain}] Step 1/5: Parsing files from {input_dir}...", flush=True)
        else:
            print("[CHECKPOINT] Skipping parsing; restoring completed document blocks.", flush=True)
        parse_start = time.time()
        parse_obs_start = log_stage_start(
            self.logger, "parse", job_id=job_id, domain=domain,
            input_dir=str(input_dir), file_count=len(files),
        )
        try:
            self.logger.info("Parsing files", step="parse", domain=domain, input_dir=str(input_dir))
            if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                canonical_blocks = self.parser.parse_folder(input_dir, domain=domain)

            parse_summary = summarize_blocks(canonical_blocks)
            if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                log_stage_end(
                    self.logger, "parse", parse_obs_start,
                    job_id=job_id, domain=domain,
                    file_count=len(files),
                    block_count=parse_summary.get("total", 0),
                    by_type=parse_summary.get("by_block_type", {}),
                    by_file_type=parse_summary.get("by_file_type", {}),
                    image_block_count=parse_summary.get("image_block_count", 0),
                    table_block_count=parse_summary.get("table_block_count", 0),
                    text_len_min=parse_summary.get("text_len_min"),
                    text_len_max=parse_summary.get("text_len_max"),
                    text_len_avg=parse_summary.get("text_len_avg"),
                )

            # Apply semantic grouping (structure-aware) before IE
            grouping_cfg = self.config.get("ingestion", {}).get("semantic_grouping", {})
            grouping_enabled = grouping_cfg.get("enabled", True)
            if grouping_enabled and restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                before_count = len(canonical_blocks)
                grouping_obs_start = log_stage_start(
                    self.logger, "grouping", job_id=job_id, domain=domain,
                    input_blocks=before_count,
                )
                max_chars = self.config["ingestion"]["max_chars_per_block"]
                canonical_blocks = group_blocks(
                    canonical_blocks,
                    max_chars_per_block=max_chars,
                    list_grouping_enabled=grouping_cfg.get("list_grouping_enabled", True),
                    paragraph_streak_enabled=grouping_cfg.get("paragraph_streak_enabled", False),
                    paragraph_streak_max_blocks=grouping_cfg.get("paragraph_streak_max_blocks", 5),
                    paragraph_streak_same_page=grouping_cfg.get("paragraph_streak_same_page", True)
                )
                self.logger.info(
                    "Semantic grouping applied",
                    domain=domain,
                    before=before_count,
                    after=len(canonical_blocks)
                )
                # Grouping diagnostics
                unit_type_counts: dict[str, int] = {}
                unit_source_sizes: dict[str, int] = {}
                for blk in canonical_blocks:
                    unit_type = getattr(blk, "unit_type", None) or "unknown"
                    unit_type_counts[unit_type] = unit_type_counts.get(unit_type, 0) + 1
                    unit_source_sizes[blk.semantic_unit_id] = len(getattr(blk, "source_block_ids", []) or [])
                multi_block_units = sum(1 for _, size in unit_source_sizes.items() if size > 1)
                total_units = len(unit_source_sizes)
                grouping_stats = {
                    "total_units": total_units,
                    "multi_block_units": multi_block_units,
                    "unit_type_counts": unit_type_counts
                }
                log_stage_end(
                    self.logger, "grouping", grouping_obs_start,
                    job_id=job_id, domain=domain,
                    input_blocks=before_count,
                    output_blocks=len(canonical_blocks),
                    semantic_units=total_units,
                    multi_block_units=multi_block_units,
                    unit_type_counts=unit_type_counts,
                )
                if total_units > 0 and multi_block_units == 0:
                    warn_msg = (
                        "Grouping produced zero multi-block semantic units. "
                        "Check parser output, list grouping, and paragraph streak settings."
                    )
                    self.logger.warning(warn_msg, domain=domain)
                    print(f"[INGEST][{domain}] WARNING: {warn_msg}", flush=True)

            # Count blocks by type
            block_types = {}
            image_count = 0
            for block in canonical_blocks:
                block_type = block.block_type
                block_types[block_type] = block_types.get(block_type, 0) + 1
                if block.image_bytes:
                    image_count += 1

            if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                print(f"[INGEST][{domain}] Step 1/5: Parsed {len(canonical_blocks)} blocks ({image_count} images)", flush=True)
            else:
                print(f"[CHECKPOINT] Restored {len(canonical_blocks)} document blocks ({image_count} images).", flush=True)
            self.logger.info(
                "Parsed blocks",
                count=len(canonical_blocks),
                step="parse",
                domain=domain,
                block_types=block_types,
                image_count=image_count,
                files_processed=len(files)
            )
            if progress_tracker:
                progress_tracker.set_counts(blocks=len(canonical_blocks), images=image_count)
                progress_tracker.set_files(len(files), len(files))
                if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                    progress_tracker.record_stage(
                        "documents.parsed", "SUCCEEDED",
                        progress={"files_done": len(files), "files_total": len(files), "blocks": len(canonical_blocks)},
                    )
                    progress_tracker.record_stage("blocks.prepared", "RUNNING", progress={"blocks": len(canonical_blocks)})
                    progress_tracker.record_stage(
                        "blocks.prepared", "SUCCEEDED",
                        progress={"blocks": len(canonical_blocks), "semantic_units": (grouping_stats or {}).get("total_units", len(canonical_blocks))},
                    )
                if restored_checkpoint_stage not in {"blocks.prepared", "lane2.pass2", "admission.plan"}:
                    progress_tracker.commit_blocks_checkpoint(
                        "blocks.prepared", canonical_blocks, block_kind="canonical"
                    )
            if not canonical_blocks:
                print(f"[INGEST][{domain}] WARNING: No blocks parsed from files", flush=True)
                self.logger.warning("No blocks parsed from files", input_dir=str(input_dir), domain=domain)
            phase_durations["parse"] = time.time() - parse_start
        except Exception as e:
            phase_durations["parse"] = time.time() - parse_start
            if progress_tracker:
                progress_tracker.record_stage(
                    "documents.parsed", "FAILED",
                    error={"code": "FILE_PROCESSING_FAILED", "message": str(e)[:500]},
                )
            error_msg = f"Failed to parse files: {str(e)}"
            self.logger.exception("Parse step failed", error=str(e), input_dir=str(input_dir), domain=domain)
            log_stage_error(self.logger, "parse", e, job_id=job_id, domain=domain,
                            input_dir=str(input_dir), started_at=parse_obs_start)
            errors.append({"step": "parse", "error": error_msg})
            canonical_blocks = []
        
        # Step 2: Enrich blocks (PII masking + Information Extraction)
        if canonical_blocks and not enriched_blocks:
            enrich_start = time.time()
            enrich_obs_start = log_stage_start(
                self.logger, "enrichment", job_id=job_id, domain=domain,
                block_count=len(canonical_blocks),
            )
            try:
                update_progress("Information Extraction", 4, 25)
                print(f"[INGEST][{domain}] Step 2/5: Enriching {len(canonical_blocks)} blocks...", flush=True)
                self.logger.info("Enriching blocks", step="enrich", domain=domain, block_count=len(canonical_blocks))
                from kbfabric_ingest.utils.ingestion_observability import trace_value_ref
                frozen_context = self._run_context
                if frozen_context is not None:
                    from kbfabric_ingest.ingest.ingestion_metadata import (
                        build_run_metadata,
                        document_id_for_source,
                    )

                    run_metadata = build_run_metadata(
                        frozen_context,
                        ingestion_batch_id=ingestion_batch_id,
                        project_ids_raw=project_ids,
                        context_tags_raw=context_tags,
                        document_nature=document_nature,
                    )
                    for block in canonical_blocks:
                        block.metadata = {**(block.metadata or {}), **run_metadata}
                for block in canonical_blocks:
                    self.logger.info(
                        "ingestion.block.trace_context",
                        job_id=job_id, ingestion_batch_id=job_id,
                        block_id=block.block_id,
                        source_file_ref=trace_value_ref(block.source_file),
                        customer_id=frozen_context.tenant_context.customer_id if frozen_context else None,
                        tenant_id=frozen_context.tenant_context.tenant_id if frozen_context else None,
                        domain_id=frozen_context.ontology.domain_id if frozen_context else domain,
                        product_line_id=frozen_context.ontology.product_line_id if frozen_context else None,
                        customer_ontology_version=frozen_context.ontology.customer_ontology_version if frozen_context else None,
                        canonical_ontology_version=frozen_context.ontology.ontology_version if frozen_context else None,
                    )
                enriched_blocks = self.ie_client.enrich_blocks(
                    canonical_blocks,
                    progress_cb=(lambda phase, pct, **kwargs: update_progress(phase, 4, pct, **kwargs)),
                    domain=domain,
                    skip_text_ie=skip_text_ie
                )
                if progress_tracker:
                    for stage_id in ("privacy.applied", "lane2.pass1", "lane2.pass2"):
                        if stage_id in progress_tracker._active_persisted_stages:
                            progress_tracker.record_stage(stage_id, "SUCCEEDED")
                    progress_tracker.commit_blocks_checkpoint(
                        "lane2.pass2", enriched_blocks, block_kind="enriched"
                    )
                    if progress_tracker.cancellation_requested():
                        raise IngestionCancelRequested("Cancellation accepted after Pass 2 checkpoint")
                print(f"[INGEST][{domain}] Step 2/5: Enriched {len(enriched_blocks)} blocks", flush=True)
                self.logger.info("Enriched blocks", count=len(enriched_blocks), step="enrich", domain=domain)

                enrich_summary = summarize_enriched_blocks(enriched_blocks)
                log_stage_end(
                    self.logger, "enrichment", enrich_obs_start,
                    job_id=job_id, domain=domain,
                    enriched_block_count=enrich_summary.get("total", 0),
                    entities=enrich_summary.get("entity_count", 0),
                    relationships=enrich_summary.get("relationship_count", 0),
                    entity_count_by_type=enrich_summary.get("entity_count_by_type", {}),
                    relationship_count_by_type=enrich_summary.get("relationship_count_by_type", {}),
                    by_block_category=enrich_summary.get("by_block_category", {}),
                    by_kg_node_type=enrich_summary.get("by_kg_node_type", {}),
                    embedding_count=enrich_summary.get("embedding_count", 0),
                    embedding_dim=enrich_summary.get("embedding_dim"),
                    pii_span_count=enrich_summary.get("pii_span_count", 0),
                )
                
                # Count entities extracted
                total_entities = sum(len(eb.candidate_entities) for eb in enriched_blocks)
                total_relationships = sum(len(eb.candidate_relationships) for eb in enriched_blocks)
                if progress_tracker:
                    progress_tracker.set_counts(entities=total_entities, relationships=total_relationships)
                
                # DIAGNOSTIC: Log entity/relationship extraction summary
                print(f"[INGEST][{domain}][DIAG] Enrichment totals: {total_entities} entities, {total_relationships} relationships across {len(enriched_blocks)} blocks", flush=True)
                
                # Count embeddings generated
                embeddings_count = sum(1 for eb in enriched_blocks if eb.embedding)
                
                embedding_dim = None
                for eb in enriched_blocks:
                    if eb.embedding:
                        try:
                            embedding_dim = len(eb.embedding)
                            break
                        except Exception:
                            embedding_dim = None
                            break
                self.logger.info(
                    "Enrichment entity/relationship totals",
                    domain=domain,
                    total_entities=total_entities,
                    total_relationships=total_relationships,
                    enriched_block_count=len(enriched_blocks),
                    embeddings_generated=embeddings_count,
                    embedding_model="Embeddings",
                    embedding_dimensions=embedding_dim
                )
                
                # DIAGNOSTIC: Check blocks with entities/relationships
                blocks_with_entities = sum(1 for eb in enriched_blocks if eb.candidate_entities)
                blocks_with_relationships = sum(1 for eb in enriched_blocks if eb.candidate_relationships)
                print(f"[INGEST][{domain}][DIAG] Blocks with content: {blocks_with_entities} have entities, {blocks_with_relationships} have relationships", flush=True)
                self.logger.info(
                    "Enrichment block distribution",
                    domain=domain,
                    blocks_with_entities=blocks_with_entities,
                    blocks_with_relationships=blocks_with_relationships
                )
                
                # DIAGNOSTIC: Sample first enriched block with entities
                for idx, eb in enumerate(enriched_blocks[:10]):
                    if eb.candidate_entities:
                        print(f"[INGEST][{domain}][DIAG] Sample block {idx}: {len(eb.candidate_entities)} entities, first={eb.candidate_entities[0] if eb.candidate_entities else 'none'}", flush=True)
                        break
                else:
                    print(f"[INGEST][{domain}][DIAG] WARNING: First 10 enriched blocks have NO candidate_entities!", flush=True)
                    self.logger.warning("First 10 enriched blocks have no candidate_entities", domain=domain)
                
                if not enriched_blocks:
                    self.logger.warning("No blocks enriched", domain=domain)
                    print(f"[INGEST][{domain}] WARNING: No blocks enriched", flush=True)
                phase_durations["enrich"] = time.time() - enrich_start
            except Exception as e:
                phase_durations["enrich"] = time.time() - enrich_start
                # Extract detailed error context for better troubleshooting
                error_str = str(e)
                error_type = type(e).__name__
                
                # Try to extract connection/retry context from error message
                error_context_parts = []
                
                # Check for connection-related errors
                if "Embedding" in error_str or "[Embedding]" in error_str:
                    error_context_parts.append(
                        "Embedding model failure - model not accessible on this worker. "
                        "Set KBFABRIC_EMBEDDINGS_MODEL_DIR or embeddings.local_model_path (offline only)."
                    )
                elif "Connection refused" in error_str or "ConnectionError" in error_str:
                    error_context_parts.append("Connection refused - Ollama server may be unavailable or overloaded")
                elif "Max retries exceeded" in error_str or "MaxRetryError" in error_str:
                    error_context_parts.append("Max retries exceeded - check Ollama server health and network connectivity")
                elif "Timeout" in error_str or "timeout" in error_str:
                    # Generic timeout - could be Ollama or internal waiters. Keep neutral.
                    error_context_parts.append("Timeout - check dependency health and worker resource saturation")
                
                # Add config context
                try:
                    config = get_app_config()
                    ollama_cfg = config.get("ollama", {})
                    text_cfg = config.get("ollama", {}).get("text", {})
                    vision_cfg = config.get("ollama", {}).get("vision", {})
                    embeddings_cfg = config.get("embeddings", {})
                    embedding_dir = (os.environ.get("KBFABRIC_EMBEDDINGS_MODEL_DIR") or "").strip() or embeddings_cfg.get("local_model_path")

                    config_context = (
                        f"Config: timeout={ollama_cfg.get('timeout_sec', 300)}s, "
                        f"text_batch_size={text_cfg.get('batch_size', 8)}, "
                        f"text_batch_delay={text_cfg.get('batch_delay_seconds', 8.0)}s, "
                        f"vision_batch_delay={vision_cfg.get('batch_delay_seconds', 10.0)}s, "
                        f"ollama_url={ollama_cfg.get('base_url', 'unknown')}, "
                        f"embedding_model_dir={embedding_dir}"
                    )
                    error_context_parts.append(config_context)
                except Exception:
                    pass
                
                # Build comprehensive error message
                if error_context_parts:
                    error_msg = f"Failed to enrich blocks: {error_str}\nContext: {' | '.join(error_context_parts)}"
                else:
                    error_msg = f"Failed to enrich blocks: {error_str} (Error type: {error_type})"
                
                print(f"[INGEST][{domain}] ERROR in enrich step: {error_msg}", flush=True)
                self.logger.exception(
                    "Enrich step failed",
                    error=error_str,
                    error_type=error_type,
                    domain=domain,
                    block_count=len(canonical_blocks) if canonical_blocks else 0
                )
                log_stage_error(self.logger, "enrichment", e, job_id=job_id, domain=domain,
                                block_count=len(canonical_blocks) if canonical_blocks else 0,
                                started_at=enrich_obs_start)
                errors.append({"step": "enrich", "error": error_msg})
                enriched_blocks = []
        elif not canonical_blocks:
            print(f"[INGEST][{domain}] Skipping enrich step - no canonical blocks", flush=True)
            self.logger.warning("Skipping enrich step - no canonical blocks", domain=domain)
        
        # Pre-seed domain spine into Neo4j (idempotent MERGE); no-op for domains without spine
        spine_counts = {}
        tenant_id = self.config["tenant_id"]
        spine_obs_start = log_stage_start(self.logger, "neo4j.spine.preseed", job_id=job_id, domain=domain, tenant_id=tenant_id)
        try:
            spine_counts = self.kg_writer.pre_seed_spine(domain, tenant_id=tenant_id)
            print(f"[INGEST][{domain}] Spine pre-seeded: {spine_counts}", flush=True)
            log_stage_end(self.logger, "neo4j.spine.preseed", spine_obs_start,
                          job_id=job_id, domain=domain, tenant_id=tenant_id, spine_counts=spine_counts)
        except Exception as e:
            self.logger.warning("Spine pre-seeding failed (non-fatal)", domain=domain, error=str(e))
            log_stage_error(self.logger, "neo4j.spine.preseed", e, job_id=job_id, domain=domain,
                            tenant_id=tenant_id, started_at=spine_obs_start)
            print(f"[INGEST][{domain}] WARNING: Spine pre-seeding failed: {e}", flush=True)

        # Domains without spine (e.g. enterprise_hr_audit_others) are Weaviate-only catch-all; skip KG/Neo4j
        domain_def = (
            self._run_context.ontology.domain_definition
            if self._run_context is not None
            else None
        )
        domain_has_spine = self._run_context is not None
        self.logger.info(
            "neo4j.routing.domain_scope",
            domain=domain,
            domain_has_spine=domain_has_spine,
            domain_definition_keys=list(domain_def.keys()) if domain_def else [],
            spine_type=type(domain_def.get("spine")).__name__ if domain_def else None,
            spine_size=len(domain_def.get("spine")) if isinstance(domain_def, dict) and isinstance(domain_def.get("spine"), list) else None,
        )

        # Complete and checkpoint graph admission before either store begins publishing.
        # This makes the immutable admission plan a safe recovery boundary and avoids
        # creating duplicate HITL work when a later worker attempt resumes.
        admitted_blocks = enriched_blocks
        admission_stats: dict[str, int] = {}
        if enriched_blocks and domain_has_spine:
            admission_stage_ids = (
                "ontology.admission", "entity.mapping",
                "relationship.resolution", "admission.plan",
            )
            if restored_checkpoint_stage == "admission.plan":
                admitted_blocks = restored_admitted_blocks
                admission_stats = restored_admission_stats
                print("[CHECKPOINT] Restored the completed admission plan.", flush=True)
            else:
                if progress_tracker:
                    for stage_id in admission_stage_ids:
                        progress_tracker.record_stage(stage_id, "RUNNING")
                admitted_blocks, admission_stats = self._admit_kg_blocks(
                    enriched_blocks,
                    domain=domain,
                    ingestion_batch_id=job_id,
                    hitl_mode=hitl_mode,
                )
                if progress_tracker:
                    for stage_id in admission_stage_ids:
                        progress_tracker.record_stage(
                            stage_id, "SUCCEEDED",
                            progress={
                                "entities_mapped": admission_stats.get("entities_mapped", 0),
                                "entities_admitted": admission_stats.get("entities_admitted", 0),
                                "relationships_admitted": admission_stats.get("relationships_admitted", 0),
                                "review_items": admission_stats.get("hitl_queued", 0),
                            },
                        )
                    progress_tracker.commit_blocks_checkpoint(
                        "admission.plan", admitted_blocks, block_kind="admitted",
                        extra_payload={
                            "admission_stats": admission_stats,
                            "enriched_blocks": [json.loads(item.model_dump_json()) for item in enriched_blocks],
                        },
                    )

        if progress_tracker and progress_tracker.cancellation_requested():
            raise IngestionCancelRequested("Cancellation accepted before store publishing")
        if progress_tracker and not progress_tracker.begin_store_publishing():
            raise IngestionCancelRequested("Cancellation won the publishing boundary")

        # Step 3-4: Parallel Write to Weaviate and Neo4j (Neo4j skipped when domain has no spine)
        update_progress("Storing in Weaviate & Neo4j", 5, 75)
        weaviate_upsert_metrics = {"before_count": 0, "after_count": 0, "new": 0, "updated": 0, "attempted": 0}
        neo4j_stats = {"nodes": 0, "relationships": 0}
        kb_ids = {}
        store_write_summary = {
            "weaviate": {"status": "SKIPPED", "chunks_written": 0, "reason": "no_enriched_blocks"},
            "neo4j": {"status": "SKIPPED", "nodes_written": 0,
                      "relationships_written": 0, "reason": "no_enriched_blocks"},
        }

        if enriched_blocks:
            weaviate_neo4j_start = time.time()
            if domain_has_spine:
                print(f"[INGEST][{domain}] Step 3-4/5: Writing {len(enriched_blocks)} blocks to Weaviate & Neo4j in parallel...", flush=True)
            else:
                print(f"[INGEST][{domain}] Step 3-4/5: Writing {len(enriched_blocks)} blocks to Weaviate (no KG/spine for this domain)...", flush=True)

            # Import threading for parallel processing
            import threading
            from typing import Any, Dict

            # Thread-safe result container to prevent race conditions
            class ThreadSafeResult:
                """Thread-safe container for inter-thread communication."""

                def __init__(self):
                    self._lock = threading.Lock()
                    self._result: Dict[str, Any] = {"success": False, "error": None, "data": None}
                    self._completed = threading.Event()

                def set_result(self, success: bool, error: str = None, data: Any = None):
                    """Atomically set the result."""
                    with self._lock:
                        self._result.update({"success": success, "error": error, "data": data})
                    self._completed.set()

                def get_result(self, timeout: int = 300) -> Dict[str, Any]:
                    """Thread-safe result retrieval with timeout."""
                    if self._completed.wait(timeout=timeout):
                        with self._lock:
                            return self._result.copy()
                    else:
                        # Timeout - return failure state
                        return {"success": False, "error": f"Timeout waiting for result ({timeout}s)", "data": None}

            # Thread-safe result containers
            weaviate_result = ThreadSafeResult()
            neo4j_result = ThreadSafeResult()

            def persist_store_unit(store_name, unit_key, status, progress, error):
                if progress_tracker:
                    progress_tracker.record_store_write_unit(
                        store_name, unit_key, status, progress=progress, error=error,
                    )

            def write_weaviate():
                """Thread function for Weaviate write."""
                try:
                    self.logger.info("Writing to Weaviate", step="weaviate", domain=domain, block_count=len(enriched_blocks))
                    result = self.weaviate_writer.write_blocks(
                        enriched_blocks, write_unit_callback=persist_store_unit,
                    )
                    weaviate_result.set_result(True, data=result)  # Atomic update
                    self.logger.info("Weaviate write completed", domain=domain)
                except Exception as e:
                    error_msg = f"Failed to write to Weaviate: {str(e)}"
                    weaviate_result.set_result(False, error=error_msg)  # Atomic update
                    self.logger.exception("Weaviate write failed", error=str(e), domain=domain)

            def write_neo4j():
                """Thread function for Neo4j write."""
                write_started = None
                try:
                    # Count entities/relationships before write
                    total_entities_before = sum(len(eb.candidate_entities) for eb in enriched_blocks)
                    total_relationships_before = sum(len(eb.candidate_relationships) for eb in enriched_blocks)
                    
                    self.logger.info(
                        "Writing to Neo4j",
                        step="neo4j",
                        domain=domain,
                        block_count=len(enriched_blocks),
                        entities_extracted=total_entities_before,
                        relationships_extracted=total_relationships_before
                    )
                    # Wait for Weaviate to complete using thread-safe result
                    weaviate_data = weaviate_result.get_result(timeout=300)

                    # Use kb_ids from Weaviate if available
                    weaviate_kb_ids = {}
                    if weaviate_data.get("success") and weaviate_data.get("data"):
                        weaviate_kb_ids = weaviate_data["data"].get("block_id_to_uuid", {})

                    self.logger.info(
                        "kg_admission.payload_ready",
                        domain=domain,
                        ingestion_batch_id=job_id,
                        admitted_block_count=len(admitted_blocks),
                        admission_stats=admission_stats,
                    )

                    run_context = self._run_context
                    write_started = log_stage_start(
                        self.logger, "neo4j.admitted_write",
                        job_id=job_id, ingestion_batch_id=job_id, domain=domain,
                        customer_id=run_context.tenant_context.customer_id if run_context else None,
                        tenant_id=run_context.tenant_context.tenant_id if run_context else None,
                        domain_id=run_context.ontology.domain_id if run_context else domain,
                        product_line_id=run_context.ontology.product_line_id if run_context else None,
                        customer_ontology_version=run_context.ontology.customer_ontology_version if run_context else None,
                        canonical_ontology_version=run_context.ontology.ontology_version if run_context else None,
                        admitted_block_count=len(admitted_blocks),
                        admitted_entity_count=admission_stats.get("entities_admitted", 0),
                        admitted_relationship_count=admission_stats.get("relationships_admitted", 0),
                    )
                    result = self.kg_writer.write_blocks(
                        admitted_blocks,
                        weaviate_kb_ids,
                        write_unit_callback=persist_store_unit,
                    )
                    if result.get("errors"):
                        raise RuntimeError(
                            f"Neo4j write had errors in {len(result['errors'])} block operations"
                        )
                    
                    nodes_written = result.get("nodes", 0)
                    relationships_written = result.get("relationships", 0)
                    self.logger.info(
                        "Neo4j write completed",
                        domain=domain,
                        nodes_written=nodes_written,
                        relationships_written=relationships_written,
                        expected_entities=total_entities_before,
                        expected_relationships=total_relationships_before
                    )
                    
                    # Verify write with retry logic (similar to Weaviate validation)
                    verified_counts = self.kg_writer.verify_write(
                        domain=domain,
                        customer_id=run_context.tenant_context.customer_id,
                        product_line_id=run_context.ontology.product_line_id,
                        ingestion_batch_id=ingestion_batch_id,
                        expected_nodes=nodes_written,
                        expected_relationships=relationships_written
                    )
                    
                    # Update result with verified counts
                    result["nodes"] = verified_counts.get("nodes", nodes_written)
                    result["relationships"] = verified_counts.get("relationships", relationships_written)
                    result["verification_status"] = verified_counts.get("verification_status", "UNAVAILABLE")
                    result["kg_admission"] = admission_stats
                    log_stage_end(
                        self.logger, "neo4j.admitted_write", write_started,
                        job_id=job_id, ingestion_batch_id=job_id, domain=domain,
                        customer_id=run_context.tenant_context.customer_id if run_context else None,
                        tenant_id=run_context.tenant_context.tenant_id if run_context else None,
                        domain_id=run_context.ontology.domain_id if run_context else domain,
                        product_line_id=run_context.ontology.product_line_id if run_context else None,
                        customer_ontology_version=run_context.ontology.customer_ontology_version if run_context else None,
                        canonical_ontology_version=run_context.ontology.ontology_version if run_context else None,
                        writer_reported_nodes=nodes_written, writer_reported_relationships=relationships_written,
                        run_scoped_nodes=result["nodes"], run_scoped_relationships=result["relationships"],
                        verification_status=result["verification_status"],
                        admission_stats=admission_stats,
                    )
                    
                    neo4j_result.set_result(True, data=result)  # Atomic update with verified counts
                except Exception as e:
                    error_msg = f"Failed to write to Neo4j: {str(e)}"
                    neo4j_result.set_result(False, error=error_msg)  # Atomic update
                    self.logger.exception("Neo4j write failed", error=str(e), domain=domain)
                    if write_started is not None:
                        log_stage_error(
                            self.logger, "neo4j.admitted_write", e,
                            job_id=job_id, ingestion_batch_id=job_id, domain=domain,
                            started_at=write_started,
                        )

            # Carry the run ID with each chunk into both persistence backends.
            # The existing metadata object is retained by Weaviate and copied
            # onto Neo4j Block provenance without adding a document model field.
            metadata_by_block_id = {
                block.block_id: dict(block.metadata or {})
                for block in canonical_blocks
                if block and block.block_id
            }
            for enriched in enriched_blocks:
                if enriched and enriched.block:
                    current_metadata = dict(enriched.block.metadata or {})
                    parent_metadata = metadata_by_block_id.get(
                        current_metadata.get("parent_block_id"), {}
                    )
                    enriched.block.metadata = {
                        **parent_metadata,
                        **current_metadata,
                        "ingestion_batch_id": ingestion_batch_id,
                    }

            # Start parallel writes (Neo4j only when domain has spine)
            weaviate_thread = threading.Thread(target=write_weaviate, name="WeaviateWriter")
            weaviate_thread.start()
            if domain_has_spine:
                neo4j_thread = threading.Thread(target=write_neo4j, name="Neo4jWriter")
                neo4j_thread.start()

            weaviate_thread.join()
            if domain_has_spine:
                neo4j_thread.join()
            else:
                neo4j_result.set_result(True, data={"nodes": 0, "relationships": 0})

            # Process Weaviate results (thread-safe retrieval)
            weaviate_data = weaviate_result.get_result(timeout=1)  # Should be completed already
            if weaviate_data["success"]:
                kb_ids = weaviate_data["data"].get("block_id_to_uuid", {})
                weaviate_upsert_metrics = weaviate_data["data"].get("upsert_metrics", weaviate_upsert_metrics)
                write_class_used = self.weaviate_writer.class_name_for_domain(domain)
                print(f"[INGEST][{domain}] Weaviate: Wrote {len(kb_ids)} blocks to class '{write_class_used}' (new: {weaviate_upsert_metrics.get('new', 0)}, updated: {weaviate_upsert_metrics.get('updated', 0)})", flush=True)
                self.logger.info("Wrote to Weaviate", count=len(kb_ids), step="weaviate", domain=domain, class_name=write_class_used, upsert_metrics=weaviate_upsert_metrics)
                if progress_tracker:
                    progress_tracker.set_counts(weaviate=len(kb_ids))
                store_write_summary["weaviate"] = {
                    "status": "SUCCEEDED", "chunks_written": len(kb_ids),
                    "new": int(weaviate_upsert_metrics.get("new", 0) or 0),
                    "updated": int(weaviate_upsert_metrics.get("updated", 0) or 0),
                }
            else:
                errors.append({"step": "weaviate", "error": weaviate_data["error"]})
                kb_ids = {}
                store_write_summary["weaviate"] = {
                    "status": "FAILED", "chunks_written": 0,
                    "error_code": "DEPENDENCY_FAILURE",
                }

            # Process Neo4j results (thread-safe retrieval)
            neo4j_data = neo4j_result.get_result(timeout=1)  # Should be completed already
            if neo4j_data["success"]:
                neo4j_stats = neo4j_data["data"]
                print(f"[INGEST][{domain}] Neo4j: Wrote {neo4j_stats.get('nodes', 0)} nodes, {neo4j_stats.get('relationships', 0)} relationships", flush=True)
                self.logger.info("Wrote to Neo4j", step="neo4j", domain=domain, nodes=neo4j_stats.get('nodes', 0), relationships=neo4j_stats.get('relationships', 0))
                if progress_tracker:
                    progress_tracker.set_counts(neo4j=neo4j_stats.get('nodes', 0))
                store_write_summary["neo4j"] = {
                    "status": "SUCCEEDED" if domain_has_spine else "SKIPPED",
                    "nodes_written": int(neo4j_stats.get("nodes", 0) or 0),
                    "relationships_written": int(neo4j_stats.get("relationships", 0) or 0),
                    "verification_status": neo4j_stats.get("verification_status", "UNAVAILABLE"),
                    **({"reason": "domain_has_no_graph_spine"} if not domain_has_spine else {}),
                }
            else:
                errors.append({"step": "neo4j", "error": neo4j_data["error"]})
                neo4j_stats = {"nodes": 0, "relationships": 0}
                store_write_summary["neo4j"] = {
                    "status": "FAILED", "nodes_written": 0,
                    "relationships_written": 0, "error_code": "DEPENDENCY_FAILURE",
                }

            # POST-INGESTION VALIDATION: Verify entity nodes were actually created (skip for Weaviate-only domains)
            if domain_has_spine:
                print(f"[INGEST][{domain}] Validating entity creation in Neo4j...", flush=True)
                try:
                    with self.kg_writer.driver.session() as validation_session:
                        entity_count_query = """
                        MATCH (n) 
                        WHERE NOT n:Block AND NOT n:Document AND NOT n:DomainRoot 
                        AND n.domain = $domain
                        RETURN count(n) as entity_count
                        """
                        result = validation_session.run(entity_count_query, domain=domain)
                        actual_entity_count = result.single()["entity_count"]
                        
                        if actual_entity_count == 0:
                            # Check if we expected entities (had candidate_entities in enriched blocks)
                            expected_entities = sum(len(eb.candidate_entities) for eb in enriched_blocks)
                            if expected_entities > 0:
                                self.logger.error(
                                    "CRITICAL: Entity extraction produced entities but none were persisted to Neo4j!",
                                    domain=domain,
                                    expected_entities=expected_entities,
                                    actual_entities=actual_entity_count
                                )
                                print(f"[INGEST][{domain}] CRITICAL: Expected {expected_entities} entities but found 0 in Neo4j!", flush=True)
                                errors.append({"step": "neo4j_validation", "error": f"Entity creation failed: expected {expected_entities}, got 0"})
                            else:
                                self.logger.info("No entities expected (all blocks were Narrative or had no extractions)", domain=domain)
                        else:
                            print(f"[INGEST][{domain}] Validated: {actual_entity_count} entity nodes in Neo4j", flush=True)
                            self.logger.info("Entity creation validated", domain=domain, entity_count=actual_entity_count)
                        
                        # VALIDATION 2: Check entity-to-entity relationships (excluding scaffolding)
                        scaffolding_rels = ['BLOCK_MENTIONS_ENTITY', 'BLOCK_BELONGS_TO_DOCUMENT', 
                                           'NODE_IN_DOMAIN', 'DOCUMENT_IN_DOMAIN']
                        relationship_query = """
                        MATCH ()-[r]->()
                        WHERE r.domain = $domain 
                          AND NOT type(r) IN $scaffolding_rels
                        RETURN count(r) as rel_count
                        """
                        rel_result = validation_session.run(relationship_query, domain=domain, scaffolding_rels=scaffolding_rels)
                        actual_rel_count = rel_result.single()["rel_count"]
                        
                        expected_rels = sum(len(eb.candidate_relationships) for eb in enriched_blocks)
                        print(f"[INGEST][{domain}] Validated: {actual_rel_count} entity relationships (expected ~{expected_rels})", flush=True)
                        self.logger.info("Relationship creation validated", domain=domain, rel_count=actual_rel_count, expected=expected_rels)
                        
                        # VALIDATION 3: Check relationship density (warn if too low)
                        if actual_entity_count > 0:
                            density = actual_rel_count / actual_entity_count
                            if density < 0.1:  # Less than 0.1 rels per entity
                                self.logger.warning(
                                    "Low relationship density - knowledge graph may be fragmented",
                                    domain=domain,
                                    entities=actual_entity_count,
                                    relationships=actual_rel_count,
                                    density=round(density, 3)
                                )
                                print(f"[INGEST][{domain}] WARNING: Low relationship density ({density:.2f} rels/entity)", flush=True)
                            else:
                                print(f"[INGEST][{domain}] Relationship density OK: {density:.2f} rels/entity", flush=True)
                except Exception as validation_error:
                    self.logger.warning("Entity validation query failed (non-fatal)", error=str(validation_error), domain=domain)

            # POST-INGESTION VALIDATION: Verify chunks were actually stored in Weaviate
            print(f"[INGEST][{domain}] Validating chunk storage in Weaviate...", flush=True)
            try:
                weaviate_class = self.weaviate_writer.class_name_for_domain(domain)
                self.logger.info("Weaviate validation", domain=domain, class_name=weaviate_class, expected_chunks=len(enriched_blocks))
                print(f"[INGEST][{domain}] Using Weaviate class: {weaviate_class}", flush=True)
                
                # First, try query without filter to verify class exists and has data
                test_query = self.weaviate_writer.client.query.get(
                    weaviate_class,
                    ["domain", "block_type"]
                ).with_limit(1).do()
                
                # Check for query errors
                if "errors" in test_query:
                    error_msg = f"Query failed: {test_query['errors']}"
                    self.logger.error("Weaviate query errors", errors=test_query["errors"], class_name=weaviate_class, domain=domain)
                    print(f"[INGEST][{domain}] ERROR: Weaviate query failed: {test_query['errors']}", flush=True)
                    errors.append({"step": "weaviate_validation", "error": error_msg})
                    stored_chunks = []
                    actual_chunk_count = 0
                else:
                    # Check if class has any data at all
                    test_chunks = test_query.get("data", {}).get("Get", {}).get(weaviate_class, [])
                    if not test_chunks:
                        self.logger.warning("Weaviate class exists but has no data", class_name=weaviate_class, domain=domain)
                        print(f"[INGEST][{domain}] WARNING: Class {weaviate_class} exists but has no chunks", flush=True)
                    
                    # Now query with domain filter - with retry logic for indexing delays
                    max_retries = 3
                    retry_delay = 2.0  # seconds
                    stored_chunks = []
                    actual_chunk_count = 0
                    
                    for attempt in range(max_retries):
                        query_result = self.weaviate_writer.client.query.get(
                            weaviate_class,
                            [
                                "source_file",
                                "domain",
                                "block_type",
                                "block_index",
                                "block_category",
                                "kg_node_type",
                            ]
                        ).with_where({
                            "path": ["domain"],
                            "operator": "Equal",
                            "valueString": domain
                        }).with_additional(["id", "vector"]).with_limit(10000).do()
                        
                        # Check for errors in filtered query
                        if "errors" in query_result:
                            self.logger.error("Weaviate filtered query errors", errors=query_result["errors"], domain=domain, attempt=attempt+1)
                            if attempt < max_retries - 1:
                                print(f"[INGEST][{domain}] Query error, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})...", flush=True)
                                time.sleep(retry_delay)
                                retry_delay *= 1.5  # Exponential backoff
                                continue
                            else:
                                print(f"[INGEST][{domain}] WARNING: Filtered query failed after {max_retries} attempts, trying fallback query without filter...", flush=True)
                                # Fallback: query all chunks in class
                                query_result = self.weaviate_writer.client.query.get(
                                    weaviate_class,
                                    ["domain", "block_type", "block_index"]
                                ).with_additional(["id"]).with_limit(10000).do()
                        
                        stored_chunks = query_result.get("data", {}).get("Get", {}).get(weaviate_class, [])
                        actual_chunk_count = len(stored_chunks)
                        
                        if actual_chunk_count > 0:
                            # Success - chunks found
                            if attempt > 0:
                                print(f"[INGEST][{domain}] Found {actual_chunk_count} chunks after {attempt + 1} attempt(s)", flush=True)
                            break
                        
                        if attempt < max_retries - 1:
                            # Wait before retry
                            print(f"[INGEST][{domain}] No chunks found yet, retrying in {retry_delay}s (attempt {attempt + 1}/{max_retries})...", flush=True)
                            time.sleep(retry_delay)
                            retry_delay *= 1.5  # Exponential backoff
                    
                    # Log domain values found (for debugging)
                    if stored_chunks:
                        domains_found = set()
                        for c in stored_chunks:
                            # Properties are at top level in Weaviate response
                            domain_val = c.get("domain")
                            if domain_val:
                                domains_found.add(domain_val)
                        if domains_found:
                            self.logger.info("Domains found in Weaviate", domains=list(domains_found), expected=domain, class_name=weaviate_class)
                            print(f"[INGEST][{domain}] Found domains in Weaviate: {list(domains_found)} (expected: {domain})", flush=True)
                        else:
                            self.logger.warning("No domain values found in stored chunks", class_name=weaviate_class)
                            print(f"[INGEST][{domain}] WARNING: No domain values found in stored chunks", flush=True)
                
                # Expected chunks = unique block IDs we attempted to upsert
                expected_chunks = len({b.block.block_id for b in enriched_blocks})
                
                # Validate chunk count WITH RETRY (eventual consistency)
                max_retries = 5
                retry_successful = False
                for attempt in range(max_retries):
                    if actual_chunk_count >= expected_chunks:
                        print(f"[INGEST][{domain}] Validated: {actual_chunk_count} chunks in Weaviate", flush=True)
                        self.logger.info("Chunk storage validated", domain=domain, chunk_count=actual_chunk_count)
                        retry_successful = True
                        break
                    
                    if attempt < max_retries - 1:
                        wait_seconds = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                        print(f"[INGEST][{domain}] Weaviate indexing in progress: {actual_chunk_count}/{expected_chunks} chunks. Retrying in {wait_seconds}s... (attempt {attempt + 1}/{max_retries})", flush=True)
                        time.sleep(wait_seconds)
                        
                        # Re-query count
                        count_result = self.weaviate_writer.client.query.aggregate(weaviate_class).with_meta_count().do()
                        actual_chunk_count = count_result["data"]["Aggregate"][weaviate_class][0]["meta"]["count"]
                
                if not retry_successful:
                    missing = expected_chunks - actual_chunk_count
                    self.logger.error(
                        "CRITICAL: Chunk write failure detected after retries",
                        domain=domain,
                        expected=expected_chunks,
                        actual=actual_chunk_count,
                        missing=missing,
                        retries=max_retries
                    )
                    print(f"[INGEST][{domain}] CRITICAL: Expected {expected_chunks} chunks but found {actual_chunk_count} after {max_retries} retries!", flush=True)
                    errors.append({"step": "weaviate_validation", "error": f"Chunk write failed: expected {expected_chunks}, got {actual_chunk_count}"})
                
                # Validate vision data storage (for image blocks)
                image_blocks = [b for b in enriched_blocks if b.block.image_bytes]
                if image_blocks:
                    # Query vision chunks separately with vision IE fields
                    vision_query = self.weaviate_writer.client.query.get(
                        weaviate_class,
                        ["block_type", "extracted_fields", "semantic_chunks"]
                    ).with_where({
                        "operator": "And",
                        "operands": [
                            {"path": ["domain"], "operator": "Equal", "valueString": domain},
                            {"path": ["block_type"], "operator": "Equal", "valueString": "image"}
                        ]
                    }).with_limit(1000).do()
                    
                    vision_chunks = vision_query.get("data", {}).get("Get", {}).get(weaviate_class, [])
                    vision_with_data = sum(
                        1 for c in vision_chunks
                        if c.get("extracted_fields") or c.get("semantic_chunks")
                    )
                    print(f"[INGEST][{domain}] Vision data: {vision_with_data}/{len(vision_chunks)} image chunks have vision IE data", flush=True)
                    
                    if vision_with_data < len(vision_chunks):
                        missing_vision = len(vision_chunks) - vision_with_data
                        self.logger.warning(
                            "Some image chunks missing vision IE data",
                            domain=domain,
                            total_images=len(vision_chunks),
                            with_vision_data=vision_with_data,
                            missing=missing_vision
                        )
                
                # Validate embeddings presence
                chunks_with_embeddings = sum(
                    1 for c in stored_chunks
                    if c.get("_additional", {}).get("vector") is not None
                )
                if chunks_with_embeddings < actual_chunk_count:
                    missing_embeddings = actual_chunk_count - chunks_with_embeddings
                    self.logger.warning(
                        "Some chunks missing embeddings",
                        domain=domain,
                        total=actual_chunk_count,
                        with_embeddings=chunks_with_embeddings,
                        missing=missing_embeddings
                    )
                    print(f"[INGEST][{domain}] WARNING: {missing_embeddings} chunks missing embeddings", flush=True)

            except Exception as weaviate_validation_error:
                self.logger.warning("Weaviate validation query failed (non-fatal)", error=str(weaviate_validation_error), domain=domain)

            phase_durations["weaviate_neo4j"] = time.time() - weaviate_neo4j_start
        else:
            self.logger.warning("Skipping Neo4j write - no enriched blocks", domain=domain)

        # Link content entities to spine, then orphan cleanup and graph metrics
        orphan_stats: dict = {}
        if domain_has_spine:
            postprocess_obs_start = log_stage_start(self.logger, "neo4j.postprocess", job_id=job_id, domain=domain)
            spine_link_stats = {}
            graph_metrics = {}
            embedding_backfill: dict = {}
            embedding_stats_before: dict = {}
            embedding_stats_after: dict = {}
            try:
                spine_link_stats = self.kg_writer.link_content_to_spine(domain, enriched_blocks)
                print(f"[INGEST][{domain}] Content-to-spine: {spine_link_stats.get('linked', 0)} LLM-linked, {spine_link_stats.get('linked_overlap', 0)} overlap-linked, {spine_link_stats.get('linked_to_root', 0)} to root, {spine_link_stats.get('unlinked', 0)} unlinked", flush=True)
            except Exception as e:
                self.logger.warning("Content-to-spine linking failed (non-fatal)", error=str(e), domain=domain)
                print(f"[INGEST][{domain}] WARNING: Content-to-spine linking failed: {e}", flush=True)

            try:
                orphan_stats = self.kg_writer.attach_orphans_to_spine(domain)
                orphans_linked = orphan_stats.get("orphans_linked_to_unlinked", 0)
                print(f"[INGEST][{domain}] Orphan safety net: {orphan_stats.get('orphans_found', 0)} found, {orphans_linked} linked to Unlinked", flush=True)
                if orphans_linked > 0:
                    neo4j_stats["nodes"] = neo4j_stats.get("nodes", 0) + 1
            except Exception as e:
                self.logger.warning("Orphan cleanup failed (non-fatal)", error=str(e), domain=domain)

            embedding_stats_before = self.kg_writer.log_entity_embedding_stats(domain, "before_backfill")
            try:
                embedding_backfill = self.kg_writer.ensure_missing_entity_embeddings(domain)
                print(
                    f"[INGEST][{domain}] Entity embedding backfill: "
                    f"scanned={embedding_backfill.get('scanned', 0)} "
                    f"embedded={embedding_backfill.get('embedded', 0)} "
                    f"skipped={embedding_backfill.get('skipped', 0)} "
                    f"failed={embedding_backfill.get('failed', 0)}",
                    flush=True,
                )
            except Exception as e:
                self.logger.warning("Entity embedding backfill failed (non-fatal)", error=str(e), domain=domain)
                embedding_backfill = {}
            embedding_stats_after = self.kg_writer.log_entity_embedding_stats(domain, "after_backfill")

            print(f"[INGEST][{domain}] Computing graph metrics (PageRank, Degree)...", flush=True)
            try:
                graph_metrics = self.kg_writer.compute_graph_metrics(domain)
                print(f"[INGEST][{domain}] Graph metrics: degree={graph_metrics.get('degree_updated', 0)}, pagerank={graph_metrics.get('pagerank_updated', 0)}", flush=True)
                self.logger.info("Computed graph metrics", domain=domain, metrics=graph_metrics)
            except Exception as e:
                self.logger.warning("Failed to compute graph metrics (non-fatal)", error=str(e), domain=domain)
                print(f"[INGEST][{domain}] WARNING: Graph metrics computation failed: {e}", flush=True)

            log_stage_end(
                self.logger, "neo4j.postprocess", postprocess_obs_start,
                job_id=job_id, domain=domain,
                linked=spine_link_stats.get("linked", 0) if isinstance(spine_link_stats, dict) else 0,
                linked_overlap=spine_link_stats.get("linked_overlap", 0) if isinstance(spine_link_stats, dict) else 0,
                linked_to_root=spine_link_stats.get("linked_to_root", 0) if isinstance(spine_link_stats, dict) else 0,
                unlinked=spine_link_stats.get("unlinked", 0) if isinstance(spine_link_stats, dict) else 0,
                orphans_found=orphan_stats.get("orphans_found", 0) if isinstance(orphan_stats, dict) else 0,
                orphans_linked=orphan_stats.get("orphans_linked_to_unlinked", 0) if isinstance(orphan_stats, dict) else 0,
                embedding_backfill_scanned=embedding_backfill.get("scanned", 0) if isinstance(embedding_backfill, dict) else 0,
                embedding_backfill_embedded=embedding_backfill.get("embedded", 0) if isinstance(embedding_backfill, dict) else 0,
                embedding_null_before=embedding_stats_before.get("embedding_null", 0) if isinstance(embedding_stats_before, dict) else 0,
                embedding_populated_after=embedding_stats_after.get("embedding_populated", 0) if isinstance(embedding_stats_after, dict) else 0,
                degree_updated=graph_metrics.get("degree_updated", 0) if isinstance(graph_metrics, dict) else 0,
                pagerank_updated=graph_metrics.get("pagerank_updated", 0) if isinstance(graph_metrics, dict) else 0,
            )

        # Sample KG paths (Retriever preview)
        sample_paths: list[dict] = []
        try:
            run_context = self._run_context
            if run_context is None:
                raise RuntimeError("Cannot sample report paths without the frozen customer scope")
            sample_paths = self.kg_writer.sample_kg_paths(
                domain=domain,
                customer_id=run_context.tenant_context.customer_id,
                product_line_id=run_context.ontology.product_line_id,
                ingestion_batch_id=ingestion_batch_id,
                limit=20,
            )
            print(f"[INGEST][{domain}] Sampled {len(sample_paths)} KG paths for report", flush=True)
        except Exception as e:
            self.logger.warning("KG path sampling failed (non-fatal)", error=str(e), domain=domain)

        # Persist independent store results after writes and validation have finished.
        store_write_summary, store_stage_status = _finalize_store_write_summary(
            store_write_summary, errors, has_blocks=bool(enriched_blocks)
        )
        if progress_tracker and store_stage_status == "SKIPPED":
            progress_tracker.record_stage(
                "stores.write", "SKIPPED", progress=store_write_summary,
                skip_reason="No enriched blocks were available to publish.",
            )
        elif progress_tracker:
            progress_tracker.record_stage(
                "stores.write", store_stage_status, progress=store_write_summary,
                error=(
                    {"code": "DEPENDENCY_FAILURE", "message": "One or more knowledge-store writes failed validation."}
                    if store_stage_status == "FAILED" else None
                ),
            )

        # Step 5: Collect statistics (always attempt, even with partial data)
        try:
            stats = self._collect_statistics(canonical_blocks, enriched_blocks)
        except Exception as e:
            error_msg = f"Failed to collect statistics: {str(e)}"
            self.logger.exception("Statistics collection failed", error=str(e), domain=domain)
            errors.append({"step": "statistics", "error": error_msg})
            stats = {
                "total_files": 0,
                "total_blocks": 0,
                "blocks_per_domain": {},
                "blocks_per_category": {},
                "pii_summary": {}
            }

        # Attach grouping diagnostics (if collected)
        stats["grouping_stats"] = grouping_stats or {}
        
        # Step 6: Generate reports
        update_progress("Generating Reports", 7, 95)
        reports_start = time.time()
        print(f"[INGEST][{domain}] Step 5/5: Generating reports...", flush=True)
        report_paths = {}
        semantic_html = ""
        audit_html = ""
        kg_html = ""
        json_report_path: str | None = None
        all_doc_metrics: list[Any] = []
        if enriched_blocks:
            try:
                self.logger.info("Generating reports", step="reports", domain=domain)
                # Add weaviate and neo4j metrics to stats before report generation
                stats["neo4j_nodes"] = neo4j_stats.get("nodes", 0)
                stats["neo4j_relationships"] = neo4j_stats.get("relationships", 0)
                stats["weaviate_chunks"] = len(kb_ids)
                stats["weaviate_upsert_metrics"] = weaviate_upsert_metrics
                stats["sample_kg_paths"] = sample_paths
                # Collect skipped images (unsupported formats) from canonical blocks for audit-only reporting
                skipped_images: list[dict[str, Any]] = []
                for cb in canonical_blocks:
                    meta = getattr(cb, "metadata", {}) or {}
                    if getattr(cb, "block_type", "") == "image_skipped" or meta.get("skipped_image"):
                        skipped_images.append({
                            "source_file": cb.source_file,
                            "block_index": cb.block_index,
                            "document_order": meta.get("document_order"),
                            "rId": meta.get("rId"),
                            "resolved_target": meta.get("resolved_target"),
                            "original_size_bytes": meta.get("original_size_bytes"),
                            "original_sha256": meta.get("original_sha256"),
                            "skip_reason": meta.get("skip_reason") or "skipped",
                        })

                json_report_path, _ = self.report_generator.generate_json_report(
                    stats,
                    enriched_blocks,
                    skipped_images=skipped_images,
                    timestamp=job_timestamp,
                    report_basename=report_basename,
                )
                semantic_html = self.report_generator.generate_html_content(
                    stats,
                    enriched_blocks,
                    skipped_images=skipped_images,
                    timestamp=job_timestamp,
                )
                if json_report_path:
                    report_paths["json"] = str(json_report_path)
                print(f"[INGEST][{domain}] Step 5/5: Reports generated", flush=True)
            except Exception as e:
                error_msg = f"Failed to generate reports: {str(e)}"
                self.logger.exception("Report generation failed", error=str(e), domain=domain)
                errors.append({"step": "reports", "error": error_msg})
                report_paths = {}
                semantic_html = ""
            
            # Collect database audit metrics (generic - shows current state)
            database_audit = None
            if enriched_blocks:
                print(f"[INGEST][{domain}] Collecting database audit metrics...", flush=True)
                try:
                    from kbfabric_ingest.reports.ie_audit import collect_database_audit_metrics
                    
                    expected_chunks = len(enriched_blocks)
                    ie_entities = sum(len(eb.candidate_entities) for eb in enriched_blocks)
                    spine_total = sum(spine_counts.values()) if spine_counts else 0
                    expected_entities = ie_entities + spine_total + 1  # +1 for DomainRoot
                    
                    database_audit = collect_database_audit_metrics(
                        domain=domain,
                        expected_chunks=expected_chunks,
                        expected_entities=expected_entities,
                        config=self.config
                    )
                    self.logger.info("Database audit metrics collected", domain=domain)
                except Exception as audit_error:
                    self.logger.warning("Database audit collection failed (non-fatal)", error=str(audit_error), domain=domain)
                    print(f"[INGEST][{domain}] WARNING: Database audit collection failed: {audit_error}", flush=True)
            
            # Generate IE Audit Report
            try:
                print(f"[INGEST][{domain}] Generating IE Audit Report...", flush=True)
                self.logger.info("Generating IE Audit Report", step="ie_audit", domain=domain)
                
                # Collect metrics grouped by document (not per-block)
                blocks_by_doc: dict[str, list[EnrichedBlock]] = {}
                for eb in enriched_blocks:
                    doc_path = eb.block.source_file
                    blocks_by_doc.setdefault(doc_path, []).append(eb)

                all_doc_metrics = []
                for doc_path, doc_blocks in blocks_by_doc.items():
                    all_doc_metrics.append(
                        collect_metrics_from_enriched_blocks(doc_blocks, doc_path, domain)
                    )
                
                # Generate IE Audit Report
                # Provenance coverage comes from KG write guarantees (relationships have source_block_id)
                neo4j_relationships = neo4j_stats.get("relationships", 0)
                provenance_coverage_pct = 100.0 if neo4j_relationships > 0 else 0.0
                
                # Build run_summary early for audit report (needed before full summary at end)
                failure_causes = getattr(self.ie_client, "_failure_causes", {}) or {}
                run_summary_for_audit = {
                    "weaviate_chunks": len(kb_ids),
                    "neo4j_nodes": neo4j_stats.get("nodes", 0),
                    "neo4j_relationships": neo4j_relationships,
                    "failure_causes": failure_causes,
                }

                ie_audit_path = generate_ie_audit_report(
                    all_doc_metrics,
                    Path("reports"),
                    timestamp=job_timestamp,
                    provenance_coverage_pct_override=provenance_coverage_pct,
                    relationships_with_provenance_override=neo4j_relationships,
                    total_relationships_override=neo4j_relationships,
                    input_dir=str(input_dir),
                    database_audit=database_audit,
                    return_html=True,
                    duration_seconds=time.time() - job_start_time,
                    run_summary=run_summary_for_audit,
                )
                
                if ie_audit_path:
                    audit_html = ie_audit_path
                    print(f"[INGEST][{domain}] IE Audit Report content prepared", flush=True)
                    self.logger.info("IE Audit Report content prepared", domain=domain)
            except Exception as e:
                error_msg = f"Failed to generate IE Audit Report: {str(e)}"
                self.logger.exception("IE Audit Report generation failed", error=str(e), domain=domain)
                errors.append({"step": "ie_audit", "error": error_msg})
                print(f"[INGEST][{domain}] WARNING: IE Audit Report failed: {e}", flush=True)
                audit_html = ""
            
            # Generate KG Graph Report
            try:
                self.logger.info("Generating KG Graph Report", step="kg_graph", domain=domain)
                kg_report_html = self.kg_report_generator.generate_html_content(
                    neo4j_driver=self.kg_writer.driver,
                    domain=domain,
                    stats=stats,
                    timestamp=job_timestamp,
                )
                if kg_report_html:
                    kg_html = kg_report_html
                    print(f"[INGEST][{domain}] KG Graph Report content prepared", flush=True)
                    self.logger.info("KG Graph Report content prepared", domain=domain)
            except Exception as e:
                error_msg = f"Failed to generate KG Graph Report: {str(e)}"
                self.logger.warning("KG Graph Report generation failed", error=str(e), domain=domain)
                print(f"[INGEST][{domain}] WARNING: KG Graph Report failed: {e}", flush=True)
                kg_html = ""
        
        # Build run summary
        neo4j_relationships = neo4j_stats.get("relationships", 0)
        relationships_with_provenance = neo4j_relationships
        provenance_coverage_pct = 100.0 if neo4j_relationships > 0 else 0.0

        # Store reconciliation: query actual counts from both stores
        store_actual = {"neo4j_nodes": 0, "neo4j_relationships": 0, "neo4j_orphans": 0, "weaviate_chunks": 0, "neo4j_by_label": {}}
        try:
            with self.kg_writer.driver.session() as session:
                node_count = session.run("MATCH (n {domain: $domain}) RETURN count(n) as c", domain=domain).single()["c"]
                rel_count = session.run("MATCH (n {domain: $domain})-[r]->() RETURN count(r) as c", domain=domain).single()["c"]
                orphan_count = session.run(
                    "MATCH (n {domain: $domain}) WHERE NOT (n)-[:PART_OF|IMPLEMENTS|IMPACTS|COVERS|VALIDATES|GOVERNED_BY|USES|VIOLATES|MISSED_BY|LINKED_TO|PRODUCES|CROSS_DOMAIN_IMPACTS|PERFORMS]->() AND NOT ()-[:PART_OF|IMPLEMENTS|IMPACTS|COVERS|VALIDATES|GOVERNED_BY|USES|VIOLATES|MISSED_BY|LINKED_TO|PRODUCES|CROSS_DOMAIN_IMPACTS|PERFORMS]->(n) AND NOT n:DomainRoot RETURN count(n) as c",
                    domain=domain
                ).single()["c"]
                label_rows = session.run("MATCH (n {domain: $domain}) RETURN labels(n)[0] as lbl, count(n) as c ORDER BY c DESC", domain=domain)
                by_label = {r["lbl"]: r["c"] for r in label_rows}
                store_actual = {"neo4j_nodes": node_count, "neo4j_relationships": rel_count, "neo4j_orphans": orphan_count, "weaviate_chunks": len(kb_ids), "neo4j_by_label": by_label}
                print(f"[INGEST][{domain}] Store reconciliation: Neo4j nodes={node_count}, rels={rel_count}, orphans={orphan_count}, Weaviate chunks={len(kb_ids)}", flush=True)
        except Exception as e:
            self.logger.warning("Store reconciliation query failed", error=str(e), domain=domain)

        run_summary = {
            "input_dir": str(input_dir),
            "domain": domain,
            "total_files": stats.get("total_files", 0),
            "total_blocks": stats.get("total_blocks", 0),
            "total_images": stats.get("total_images", 0),
            "vector_diagrams": stats.get("vector_diagrams", 0),
            "blocks_per_domain": stats.get("blocks_per_domain", {}),
            "blocks_per_category": stats.get("blocks_per_category", {}),
            "pii_summary": stats.get("pii_summary", {}),
            "neo4j_nodes": neo4j_stats.get("nodes", 0),
            "neo4j_relationships": neo4j_relationships,
            "relationships_extracted": stats.get("relationships_extracted", 0),
            "kg_admission": neo4j_stats.get("kg_admission", {}),
            "relationships_with_provenance": relationships_with_provenance,
            "provenance_coverage_pct": provenance_coverage_pct,
            "weaviate_chunks": len(kb_ids),
            "weaviate_upsert_metrics": weaviate_upsert_metrics,
            "entities_extracted": stats.get("entities_extracted", 0),
            "store_actual": store_actual,
            "store_write_summary": store_write_summary,
            "sample_kg_paths": sample_paths,
            "orphan_attachment": orphan_stats,
            "spine_counts": spine_counts if 'spine_counts' in locals() else {},
            "weaviate_schema_validation": getattr(self, "weaviate_schema_validation", {}),
            "neo4j_index_validation": getattr(self, "neo4j_index_validation", {}),
            "phase_durations": phase_durations,
            "failure_causes": getattr(self.ie_client, "_failure_causes", {}) or {},
            "report_paths": report_paths
        }
        # The report needs the immutable customer scope and the durable review/stage
        # snapshot; do not infer these values from document content or global stores.
        if self._run_context is not None:
            tenant_context = self._run_context.tenant_context
            ontology_context = self._run_context.ontology
            run_summary["scope"] = {
                "customer_id": tenant_context.customer_id,
                "customer_name": tenant_context.customer_name,
                "tenant_id": tenant_context.tenant_id,
                "industry_id": tenant_context.industry_id,
                "domain_id": ontology_context.domain_id,
                "product_line_id": ontology_context.product_line_id,
                "product_line_name": ontology_context.product_line_name,
                "customer_ontology_id": str(ontology_context.customer_ontology_id),
                "customer_ontology_version": ontology_context.customer_ontology_version,
                "canonical_ontology_id": ontology_context.ontology_id,
                "canonical_ontology_version": ontology_context.ontology_version,
                "extraction_policy_version": str(
                    (ontology_context.extraction_policy or {}).get("version")
                    or (ontology_context.extraction_policy or {}).get("policy_version")
                    or ""
                ),
            }
        run_summary["document_nature"] = document_nature
        run_summary["hitl_mode"] = str(hitl_mode.value if hasattr(hitl_mode, "value") else hitl_mode or "")
        submission_metadata = {}
        if progress_tracker:
            submission_metadata = (
                (progress_tracker.state.get("audit_context") or {}).get("submission_metadata") or {}
            )
        if not submission_metadata and enriched_blocks:
            submission_metadata = enriched_blocks[0].block.metadata or {}
        run_summary["project_ids"] = submission_metadata.get("project_ids", [])
        run_summary["context_tags"] = submission_metadata.get("context_tags", [])
        if progress_tracker and job_id:
            try:
                persisted = progress_tracker._repository().get_job_details(job_id) or {}
                run_summary["job_details"] = {
                    key: persisted.get(key) for key in (
                        "job_id", "run_id", "ingestion_batch_id", "submitted_by", "auth_source", "submitted_at",
                        "status", "stages", "hitl_summary", "pending_review_count",
                        "store_reconciliation_required", "hitl_mode",
                    ) if key in persisted
                }
                report_scope = run_summary.get("scope") or {}
                batch_id = persisted.get("ingestion_batch_id") or ingestion_batch_id
                domain_id = report_scope.get("domain_id") or persisted.get("domain_id") or domain
                product_line_id = report_scope.get("product_line_id") or persisted.get("product_line_id")
                if batch_id and domain_id and product_line_id:
                    run_summary["job_details"]["review_audit"] = HITLReviewRepository().list_report_audit(
                        ingestion_batch_id=str(batch_id),
                        domain_id=str(domain_id),
                        product_line_id=str(product_line_id),
                    )
                run_summary["job_details"]["audit_snapshot_at"] = datetime.now().astimezone().isoformat()
            except Exception as exc:
                self.logger.warning(
                    "Could not attach persisted stage/review snapshot to report",
                    job_id=job_id, error=str(exc),
                )
        if job_id:
            run_summary["job_id"] = job_id
        
        # Add error information if any errors occurred
        if errors:
            run_summary["errors"] = errors
            # If critical steps failed, add top-level error
            critical_steps_failed = [
                e for e in errors
                if e["step"] in [
                    "parse", "enrich", "weaviate", "neo4j",
                    "neo4j_validation", "weaviate_validation",
                ]
            ]
            if critical_steps_failed:
                run_summary["error"] = f"Ingestion failed at step(s): {', '.join([e['step'] for e in critical_steps_failed])}"
        
        if errors:
            self.logger.warning("Ingestion completed with errors", summary=run_summary, error_count=len(errors))
        else:
            self.logger.info("Ingestion completed successfully", summary=run_summary)
        
        # Record telemetry metrics
        job_duration = time.time() - job_start_time
        status = "error" if errors else "success"
        run_summary["status"] = status  # Add status to return dict
        record_job_end(domain, status, job_duration)

        # Reports phase duration so far (content generation)
        phase_durations["reports"] = time.time() - reports_start

        # Generate unified HTML report (single output)
        unified_start = time.time()
        report_generation_error: str | None = None
        try:
            if not semantic_html:
                semantic_html = "<div class='container'><h2>Semantic Report Unavailable</h2><p>No semantic report content was generated for this run.</p></div>"
            if not audit_html:
                audit_html = "<div class='container'><h2>Audit Report Unavailable</h2><p>No audit report content was generated for this run.</p></div>"
            if not kg_html:
                kg_html = "<div class='container'><h2>KG Report Unavailable</h2><p>No knowledge graph report content was generated for this run.</p></div>"

            if progress_tracker:
                from dataclasses import fields
                report_metrics = []
                for metric in all_doc_metrics:
                    report_metrics.append({
                        field.name: sorted(getattr(metric, field.name))
                        if field.name == "pii_types_found"
                        else getattr(metric, field.name)
                        for field in fields(metric)
                        if field.name != "block_metrics"
                    })
                report_payload = {
                    "payload_kind": "report_inputs",
                    "domain": domain,
                    "timestamp": job_timestamp,
                    "job_id": job_id,
                    "run_summary": run_summary,
                    "document_metrics": report_metrics,
                    "semantic_html": semantic_html,
                    "audit_html": audit_html,
                    "kg_html": kg_html,
                    "json_report_path": str(json_report_path) if json_report_path else None,
                    "log_file_path": str(job_output_log_path or job_log_file),
                }
                report_payload = json.loads(json.dumps(report_payload, default=str))
                progress_tracker.commit_payload_checkpoint(
                    "stores.write", "report_inputs", report_payload,
                )

            # Print end-of-log marker and flush so the log tab has complete content
            print(f"\n{'='*70}", flush=True)
            print(f"  INGESTION LOG END — Report generation follows", flush=True)
            print(f"  Domain: {domain} | Status: {'SUCCESS' if not errors else 'ERRORS'} | Blocks: {run_summary.get('total_blocks', 0)}", flush=True)
            print(f"  Neo4j: {store_actual.get('neo4j_nodes', '?')} nodes, {store_actual.get('neo4j_relationships', '?')} rels, {store_actual.get('neo4j_orphans', '?')} orphans", flush=True)
            print(f"  Weaviate: {store_actual.get('weaviate_chunks', '?')} chunks", flush=True)
            print(f"{'='*70}\n", flush=True)
            try:
                file_handler.flush()
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass

            unified_path = self.unified_report_generator.generate(
                domain=domain,
                timestamp=job_timestamp,
                job_id=job_id,
                run_summary=run_summary,
                document_metrics=all_doc_metrics,
                semantic_html=semantic_html,
                audit_html=audit_html,
                kg_html=kg_html,
                json_report_path=str(json_report_path) if json_report_path else None,
                log_file_path=str(job_output_log_path or job_log_file),
                output_dir=Path("reports"),
            )
            report_paths["unified"] = str(unified_path)
            self.logger.info("Unified report generated", path=str(unified_path), domain=domain)
            self.logger.info(
                "ingestion.report.generated",
                job_id=job_id, domain=domain,
                report_path=str(unified_path),
                json_report_path=str(json_report_path) if json_report_path else None,
            )
        except Exception as e:
            self.logger.warning("Unified report generation failed", error=str(e), domain=domain)
            report_generation_error = str(e)
        phase_durations["reports"] = phase_durations.get("reports", 0) + (time.time() - unified_start)
        if report_generation_error:
            errors.append({"step": "reports", "error": report_generation_error})
            run_summary["error"] = f"Report generation failed: {report_generation_error}"
            run_summary["status"] = "error"
        
        # Record block counts by category
        for category, count in run_summary.get("blocks_per_category", {}).items():
            record_blocks_processed(domain, category, count)
        
        # Record entity counts
        total_entities = run_summary.get("entities_extracted", 0)
        if total_entities > 0:
            record_entities_extracted(domain, "total", total_entities)
        
        # Update span with final metrics
        span.set_attribute("total_blocks", run_summary.get("total_blocks", 0))
        span.set_attribute("total_files", run_summary.get("total_files", 0))
        span.set_attribute("neo4j_nodes", run_summary.get("neo4j_nodes", 0))
        span.set_attribute("weaviate_chunks", run_summary.get("weaviate_chunks", 0))
        span.set_attribute("duration_seconds", job_duration)
        span.set_attribute("status", status)
        
        # Copy job log to reports directory for persistence
        try:
            if report_paths:
                # Get report directory from semantic ingestion report path
                semantic_report_path = report_paths.get("semantic_ingestion")
                if semantic_report_path:
                    report_dir = Path(semantic_report_path).parent
                    log_copy_path = report_dir / f"ingestion_{domain}_{job_timestamp}.log"
                    shutil.copy2(job_log_file, log_copy_path)
                    self.logger.info(f"Job log copied to reports: {log_copy_path}")
                    run_summary["log_file"] = str(log_copy_path)
        except Exception as e:
            self.logger.warning(f"Failed to copy log file to reports: {e}")
        
        # Log final summary
        phase_log = ""
        if phase_durations:
            phase_log = "\nPhase durations: " + ", ".join(
                f"{k}={v:.2f}s" for k, v in sorted(phase_durations.items()) if v is not None
            )
        self.logger.info(
            "\n" + "="*80 + "\n" +
            f"INGESTION JOB COMPLETED: {job_timestamp}\n" +
            f"Domain: {domain}\n" +
            f"Status: {status.upper()}\n" +
            f"Duration: {job_duration:.2f}s\n" +
            f"{phase_log}\n" +
            f"Files: {run_summary.get('total_files', 0)}\n" +
            f"Blocks Processed: {run_summary.get('total_blocks', 0)}\n" +
            f"Images: {run_summary.get('total_images', 0)}\n" +
            f"Weaviate Chunks: {run_summary.get('weaviate_chunks', 0)}\n" +
            f"Neo4j Nodes: {run_summary.get('neo4j_nodes', 0)}\n" +
            f"Errors: {len(errors)}\n" +
            f"Log File: {job_log_file}\n" +
            "="*80
        )
        
        # Cleanup file handler
        try:
            file_handler.flush()
            file_handler.close()
            root_logger.removeHandler(file_handler)
            self.logger.removeHandler(file_handler)
        except Exception as e:
            print(f"Warning: Failed to cleanup file handler: {e}", flush=True)
        
        return run_summary

    def resume_report_from_checkpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Regenerate only the unified report from immutable persisted report inputs."""
        if payload.get("payload_kind") != "report_inputs":
            raise ValueError("Report recovery checkpoint payload is invalid")
        from kbfabric_ingest.reports.ie_audit import IEDocumentMetrics
        document_metrics = []
        for item in payload.get("document_metrics", []):
            metric = dict(item)
            metric["pii_types_found"] = set(metric.get("pii_types_found") or [])
            document_metrics.append(IEDocumentMetrics(**metric))
        path = self.unified_report_generator.generate(
            domain=payload["domain"], timestamp=payload["timestamp"],
            job_id=payload.get("job_id"), run_summary=payload.get("run_summary") or {},
            document_metrics=document_metrics,
            semantic_html=payload.get("semantic_html") or "",
            audit_html=payload.get("audit_html") or "",
            kg_html=payload.get("kg_html") or "",
            json_report_path=payload.get("json_report_path"),
            log_file_path=payload.get("log_file_path"), output_dir=Path("reports"),
        )
        return {"report_paths": {"unified": str(path)}, "error": None}

    def _collect_statistics(
        self,
        canonical_blocks: list,
        enriched_blocks: list[EnrichedBlock]
    ) -> dict[str, Any]:
        """
        Collect statistics from blocks with robust error handling.
        
        Args:
            canonical_blocks: List of CanonicalBlock instances (may be empty or None)
            enriched_blocks: List of EnrichedBlock instances (may be empty or None)
            
        Returns:
            Dictionary with statistics, always returns valid dict structure
        """
        # Validate inputs
        if canonical_blocks is None:
            self.logger.warning("canonical_blocks is None, using empty list")
            canonical_blocks = []
        if enriched_blocks is None:
            self.logger.warning("enriched_blocks is None, using empty list")
            enriched_blocks = []
        
        if not isinstance(canonical_blocks, list):
            self.logger.warning(f"canonical_blocks is not a list (type: {type(canonical_blocks)}), using empty list")
            canonical_blocks = []
        if not isinstance(enriched_blocks, list):
            self.logger.warning(f"enriched_blocks is not a list (type: {type(enriched_blocks)}), using empty list")
            enriched_blocks = []
        
        # Count files from canonical blocks
        total_files = 0
        try:
            source_files = set()
            for block in canonical_blocks:
                if block is None:
                    continue
                if hasattr(block, 'source_file') and block.source_file:
                    source_files.add(block.source_file)
            total_files = len(source_files)
        except Exception as e:
            self.logger.warning(f"Failed to count files: {e}")
            total_files = 0
        
        # Count blocks per domain
        blocks_per_domain: dict[str, int] = {}
        try:
            for block in enriched_blocks:
                if block is None:
                    self.logger.warning("Found None in enriched_blocks list, skipping")
                    continue
                if not hasattr(block, 'block') or block.block is None:
                    self.logger.warning("EnrichedBlock has None block attribute, skipping")
                    continue
                domain = getattr(block.block, 'domain', None)
                if domain is None:
                    self.logger.warning("Block has None domain, skipping")
                    continue
                blocks_per_domain[domain] = blocks_per_domain.get(domain, 0) + 1
        except Exception as e:
            self.logger.warning(f"Failed to count blocks per domain: {e}")
            blocks_per_domain = {}
        
        # Count blocks per category
        blocks_per_category: dict[str, int] = {}
        try:
            for block in enriched_blocks:
                if block is None:
                    continue
                if not hasattr(block, 'block_category') or block.block_category is None:
                    self.logger.warning("EnrichedBlock has None block_category, skipping")
                    continue
                try:
                    category = block.block_category.value
                    blocks_per_category[category] = blocks_per_category.get(category, 0) + 1
                except (AttributeError, ValueError) as e:
                    self.logger.warning(f"Failed to get category value: {e}")
                    continue
        except Exception as e:
            self.logger.warning(f"Failed to count blocks per category: {e}")
            blocks_per_category = {}
        
        # Count PII occurrences
        pii_summary: dict[str, int] = {}
        try:
            for block in enriched_blocks:
                if block is None:
                    continue
                if not hasattr(block, 'pii_spans') or not block.pii_spans:
                    continue
                for span in block.pii_spans:
                    if span is None:
                        continue
                    if not isinstance(span, dict):
                        continue
                    entity_type = span.get("entity_type", "UNKNOWN")
                    pii_summary[entity_type] = pii_summary.get(entity_type, 0) + 1
        except Exception as e:
            self.logger.warning(f"Failed to count PII: {e}")
            pii_summary = {}
        
        # Count extracted entities
        total_entities = 0
        try:
            for block in enriched_blocks:
                if block is None:
                    continue
                if hasattr(block, 'candidate_entities') and block.candidate_entities:
                    total_entities += len(block.candidate_entities)
        except Exception as e:
            self.logger.warning(f"Failed to count entities: {e}")
            total_entities = 0
        
        # Count relationships extracted
        total_relationships = 0
        try:
            for block in enriched_blocks:
                if block is None:
                    continue
                if hasattr(block, 'candidate_relationships') and block.candidate_relationships:
                    total_relationships += len(block.candidate_relationships)
        except Exception as e:
            self.logger.warning(f"Failed to count relationships: {e}")
            total_relationships = 0
        
        # Count images from canonical blocks (separate normal vs vector diagrams)
        total_images = 0
        vector_diagrams = 0
        try:
            for block in canonical_blocks:
                if block is None:
                    continue
                if hasattr(block, 'image_bytes') and block.image_bytes:
                    total_images += 1
                    # Check if this is a full-page raster (vector diagram)
                    metadata = getattr(block, 'metadata', {}) or {}
                    if metadata.get("rasterization") == "full_page" or metadata.get("source") == "pdf_full_page_raster_vector_diagram":
                        vector_diagrams += 1
        except Exception as e:
            self.logger.warning(f"Failed to count images: {e}")
            total_images = 0
            vector_diagrams = 0

        return {
            "total_files": total_files,
            "total_blocks": len(enriched_blocks),
            "total_images": total_images,
            "vector_diagrams": vector_diagrams,
            "blocks_per_domain": blocks_per_domain,
            "blocks_per_category": blocks_per_category,
            "pii_summary": pii_summary,
            "entities_extracted": total_entities,
            "relationships_extracted": total_relationships,
            "weaviate_chunks": len(enriched_blocks),  # Assume 1 chunk per block
            "neo4j_nodes": len(enriched_blocks),      # Assume 1 node per block
            "neo4j_relationships": total_relationships
        }



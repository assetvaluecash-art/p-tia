"""Reusable pipeline step functions (classify → extract → map).

Each ``step_*`` function does one job and can be called independently.
``run_*`` helpers compose steps for common lane flows.
"""

from __future__ import annotations

from typing import Any, Sequence

from kbfabric_ingest.ingest.entity_mapping import (
    EntityMappingResult,
    annotate_entities_with_mapping,
)
from kbfabric_ingest.ingest.lane2_two_pass import (
    ClassificationResult,
    ClassifiedNodeType,
    ExtractionResult,
    Lane2TwoPassResult,
    classify_block,
    extract_for_category,
    lane2_result_to_ie_dict,
    merge_extraction_results,
    process_lane2_block,
)
from kbfabric_ingest.ingest.models import CanonicalBlock
from kbfabric_ingest.ingest.pipeline.context import PipelineBlockResult, PipelineDeps
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Atomic steps
# ---------------------------------------------------------------------------


def step_classify(
    block: CanonicalBlock,
    deps: PipelineDeps,
    *,
    masked_text: str | None = None,
) -> ClassificationResult:
    """Step: Lane 2 multi-type classification (reusable).

    Identifies one or more ontology node types present in the block.
    Not used for Lane 1 / Lane 3 (types already known).
    """
    if deps.llm_callable is None:
        return ClassificationResult(
            category=None,
            categories=[],
            unmatched=True,
            validation_errors=["missing_llm_callable"],
            reasoning_summary="PipelineDeps.llm_callable is required for classify",
        )
    result = classify_block(
        block,
        llm_callable=deps.llm_callable,
        ontology=deps.ontology,
        masked_text=masked_text,
        review_io=deps.review_io,
    )
    logger.info(
        "pipeline.step.classify",
        block_id=block.block_id,
        categories=result.identified_categories,
        unmatched=result.unmatched,
        is_valid=result.is_valid,
    )
    return result


def step_extract_for_types(
    block: CanonicalBlock,
    categories: Sequence[str] | Sequence[ClassifiedNodeType],
    deps: PipelineDeps,
    *,
    classification: ClassificationResult | None = None,
    masked_text: str | None = None,
) -> tuple[list[ExtractionResult], list[dict[str, Any]]]:
    """Step: type-specific extraction for every identified category.

    Returns ``(extractions, failures)``. A failure on one type does not
    discard successful extractions for other types.
    """
    if deps.llm_callable is None:
        return [], [{"error": "missing_llm_callable"}]
    if deps.prompt_provider is None:
        return [], [{"error": "missing_prompt_provider"}]

    names: list[str] = []
    for item in categories:
        if isinstance(item, ClassifiedNodeType):
            names.append(item.category)
        else:
            names.append(str(item))

    extractions: list[ExtractionResult] = []
    failures: list[dict[str, Any]] = []
    for cat in names:
        try:
            one = extract_for_category(
                block,
                cat,
                llm_callable=deps.llm_callable,
                prompt_provider=deps.prompt_provider,
                ontology=deps.ontology,
                masked_text=masked_text,
                classification=classification,
                review_io=deps.review_io,
            )
            extractions.append(one)
            if one.failed:
                failures.append(
                    {
                        "classified_node_type": cat,
                        "validation_errors": list(one.validation_errors),
                        "prompt_id": one.prompt_id,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "pipeline.step.extract_failed",
                block_id=block.block_id,
                extraction_type=cat,
                error=str(exc),
            )
            failures.append({"classified_node_type": cat, "error": str(exc)})

    logger.info(
        "pipeline.step.extract",
        block_id=block.block_id,
        categories=names,
        extraction_call_count=len(extractions),
        failure_count=len(failures),
    )
    return extractions, failures


def step_merge_extractions(
    classification: ClassificationResult,
    extractions: list[ExtractionResult],
) -> ExtractionResult:
    """Step: merge per-type extractions into one block-level result."""
    return merge_extraction_results(classification, extractions)


from kbfabric_ingest.ingest.execution_gate import (
    HITLMode,
    evaluate_execution_gate,
)
from kbfabric_ingest.ingest.execution_plan import (
    ExecutionPlan,
    build_execution_plan,
)


def step_build_execution_plans(
    mapping_results: list[EntityMappingResult],
    *,
    hitl_mode: HITLMode = HITLMode.REVIEW_AMBIGUOUS,
    ingestion_batch_id: str | None = None,
    ontology_version: str | None = None,
    retrieval_version: str | None = None,
    ranking_version: str | None = None,
) -> list[ExecutionPlan]:
    """Build deterministic Neo4j admission plans from mapping results.

    This stage performs no external writes. Weaviate ingestion remains
    independent of this KG admission decision.
    """
    plans: list[ExecutionPlan] = []

    for index, mapping in enumerate(mapping_results):
        gate = evaluate_execution_gate(
            mapping,
            hitl_mode=hitl_mode,
        )

        plan = build_execution_plan(
            mapping=mapping,
            gate=gate,
            # The same stable block can be ingested in more than one run.
            # Keep retries/resumes idempotent within a batch while preventing
            # the globally unique HITL plan key from aliasing another run.
            plan_id=(
                f"exec-{ingestion_batch_id}-{mapping.incoming.block_id}-{index}"
                if ingestion_batch_id
                else f"exec-{mapping.incoming.block_id}-{index}"
            ),
            ontology_version=ontology_version,
            retrieval_version=retrieval_version,
            ranking_version=ranking_version,
        )

        plans.append(plan)

    counts = {
        "WRITE_NOW": sum(
            1 for plan in plans if plan.action.value == "WRITE_NOW"
        ),
        "WRITE_UNRESOLVED": sum(
            1 for plan in plans if plan.action.value == "WRITE_UNRESOLVED"
        ),
        "QUEUE_FOR_REVIEW": sum(
            1 for plan in plans if plan.action.value == "QUEUE_FOR_REVIEW"
        ),
        "REJECT": sum(
            1 for plan in plans if plan.action.value == "REJECT"
        ),
    }

    logger.info(
        "pipeline.step.execution_plans.built",
        log_kind="output",
        mapping_count=len(mapping_results),
        plan_count=len(plans),
        hitl_mode=hitl_mode.value,
        **counts,
    )

    return plans


def step_map_entities(
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    *,
    block: CanonicalBlock,
    lane: str,
    deps: PipelineDeps,
    provenance: dict[str, Any] | None = None,
    batch_candidates: list[Any] | None = None,
    source_context: str = "",
) -> tuple[list[dict[str, Any]], list[EntityMappingResult]]:
    """Step: existing-node mapping / dedup / delta decisions (all lanes).

    Safe to call for Lane 1, Lane 2, and Lane 3. When mapping is disabled
    in deps, entities are returned unchanged.
    """
    if not deps.mapping_enabled or not entities:
        return list(entities or []), []

    annotated, results = annotate_entities_with_mapping(
        entities,
        relationships,
        lane=lane,
        block=block,
        ontology=deps.ontology,
        candidate_reader=deps.candidate_reader,
        semantic=deps.semantic,
        llm_callable=deps.llm_callable,
        # Candidate identity decisions use the shared statistical policy.
        # Keep the dependency field for config compatibility, but do not allow
        # it to re-enable LLM adjudication from this pipeline boundary.
        use_llm_adjudication=False,
        provenance={**deps.run_scope, **(provenance or {})},
        batch_candidates=batch_candidates,
        reranker=deps.reranker,
        context_evidence_reader=deps.context_evidence_reader,
        source_context=source_context,
    )
    logger.info(
        "pipeline.step.map",
        log_kind="output",
        lane=lane,
        block_id=block.block_id,
        entity_count=len(annotated),
        reused_count=sum(1 for r in results if r.selected_entity_id),
        create_new_count=sum(1 for r in results if r.decision.value == "CREATE_NEW"),
        requires_review_count=sum(
            1 for r in results if r.decision.value == "REQUIRES_REVIEW"
        ),
        scores_passed_to_llm=False,
        per_entity=[
            {
                "name": r.incoming.name,
                "type": r.incoming.node_type,
                "decision": r.decision.value,
                "lexical_score": r.score_breakdown.lexical_score,
                "semantic_score": r.score_breakdown.semantic_score,
                "fusion_score": r.score_breakdown.fusion_score,
                "selected_entity_id": r.selected_entity_id,
            }
            for r in results
        ],
    )
    print(
        f"[EntityMapping][OUTPUT] step_map lane={lane} block={block.block_id} "
        f"entities={len(annotated)} reused={sum(1 for r in results if r.selected_entity_id)} "
        "scores_to_llm=False",
        flush=True,
    )
    return annotated, results


# ---------------------------------------------------------------------------
# Composed runners
# ---------------------------------------------------------------------------


def run_lane2_ie(
    block: CanonicalBlock,
    deps: PipelineDeps,
    *,
    masked_text: str | None = None,
) -> Lane2TwoPassResult:
    """Compose classify → multi-extract → merge (Lane 2 only, no mapping)."""
    if deps.llm_callable is None:
        raise ValueError("PipelineDeps.llm_callable is required for run_lane2_ie")
    return process_lane2_block(
        block,
        llm_callable=deps.llm_callable,
        prompt_provider=deps.prompt_provider,
        ontology=deps.ontology,
        masked_text=masked_text,
        review_io=deps.review_io,
    )


def run_lane2_ie_and_map(
    block: CanonicalBlock,
    deps: PipelineDeps,
    *,
    masked_text: str | None = None,
    lane: str = "lane2",
) -> PipelineBlockResult:
    """Full Lane 2 utility: classify → extract×N → merge → map entities."""
    steps: list[str] = []
    two_pass = run_lane2_ie(block, deps, masked_text=masked_text)
    steps.extend(["classify", "extract", "merge"])

    ie_dict = lane2_result_to_ie_dict(two_pass, deps.ontology)
    entities = list(ie_dict.get("entities") or [])
    relationships = list(ie_dict.get("relationships") or [])

    mapping_results: list[EntityMappingResult] = []
    if deps.mapping_enabled and entities:
        entities, mapping_results = step_map_entities(
            entities,
            relationships,
            block=block,
            lane=lane,
            deps=deps,
        )
        steps.append("map")
        ie_dict = {**ie_dict, "entities": entities, "relationships": relationships}

    return PipelineBlockResult(
        lane=lane,
        block_id=block.block_id,
        source_file=block.source_file,
        classification=two_pass.classification,
        extractions=list(two_pass.extractions or []),
        extraction=two_pass.extraction,
        two_pass=two_pass,
        entities=entities,
        relationships=relationships,
        mapping_results=mapping_results,
        ie_dict=ie_dict,
        success=two_pass.success,
        failure_reason=two_pass.failure_reason,
        steps_run=steps,
    )


def run_map_only(
    block: CanonicalBlock,
    *,
    lane: str,
    entities: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    deps: PipelineDeps,
    classification: ClassificationResult | None = None,
) -> PipelineBlockResult:
    """Utility for Lane 1 / Lane 3: skip classify; map already-extracted entities."""
    annotated, mapping_results = step_map_entities(
        entities,
        relationships,
        block=block,
        lane=lane,
        deps=deps,
    )
    return PipelineBlockResult(
        lane=lane,
        block_id=block.block_id,
        source_file=block.source_file,
        classification=classification,
        entities=annotated,
        relationships=list(relationships or []),
        mapping_results=mapping_results,
        ie_dict={
            "entities": annotated,
            "relationships": relationships,
        },
        success=True,
        steps_run=["map"] if deps.mapping_enabled else [],
    )

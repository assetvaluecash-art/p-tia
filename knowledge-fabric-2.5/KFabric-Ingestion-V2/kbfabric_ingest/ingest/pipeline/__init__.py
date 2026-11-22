"""Reusable ingestion pipeline step utilities.

These steps are lane-agnostic building blocks. Call them from ``IEClient``,
demos, tests, or any other orchestrator without depending on enrich_blocks.

Typical Lane 2 flow::

    from kbfabric_ingest.ingest.pipeline import (
        PipelineDeps,
        step_classify,
        step_extract_for_types,
        step_map_entities,
        run_lane2_ie_and_map,
    )

    deps = PipelineDeps.from_clients(llm_client=..., ontology=...)
    result = run_lane2_ie_and_map(block, deps, masked_text=masked)

Lane 1 / Lane 3 (type already known)::

    annotated, mapping = step_map_entities(
        entities, relationships, block=block, lane="lane1", deps=deps
    )
"""

from __future__ import annotations

from kbfabric_ingest.ingest.pipeline.context import PipelineDeps, PipelineBlockResult
from kbfabric_ingest.ingest.pipeline.ingestion_pipeline_steps import (
    run_lane2_ie,
    run_lane2_ie_and_map,
    run_map_only,
    step_classify,
    step_extract_for_types,
    step_map_entities,
    step_merge_extractions,
)

__all__ = [
    "PipelineDeps",
    "PipelineBlockResult",
    "step_classify",
    "step_extract_for_types",
    "step_merge_extractions",
    "step_map_entities",
    "run_lane2_ie",
    "run_lane2_ie_and_map",
    "run_map_only",
]

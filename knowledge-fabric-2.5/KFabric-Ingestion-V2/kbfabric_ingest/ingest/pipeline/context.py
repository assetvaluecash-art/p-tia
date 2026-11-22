"""Shared dependency bag for reusable pipeline steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.ingest.entity_mapping import EntityMappingResult
from kbfabric_ingest.ingest.lane2_two_pass import (
    ClassificationResult,
    ExtractionResult,
    Lane2TwoPassResult,
    LLMCallable,
    YamlOntologyProvider,
    build_lane2_ontology,
    make_llm_callable_from_client,
)
from kbfabric_ingest.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PipelineDeps:
    """Injectable dependencies for classify / extract / map steps.

    Build once per job (or lazily) and pass into every step so lanes and
    external callers share the same Neo4j reader, embeddings, and ontology.
    """

    ontology: dict[str, Any] = field(default_factory=dict)
    run_scope: dict[str, Any] = field(default_factory=dict)
    llm_callable: LLMCallable | None = None
    prompt_provider: Any | None = None
    candidate_reader: Any | None = None
    semantic: Any | None = None
    reranker: Any | None = None
    context_evidence_reader: Any | None = None
    mapping_enabled: bool = True
    use_llm_adjudication: bool = False
    review_io: bool = False

    @classmethod
    def from_config(
        cls,
        *,
        ontology: dict[str, Any] | None = None,
        canonical_model: dict[str, Any] | None = None,
        extraction_policy: dict[str, Any] | None = None,
        run_scope: dict[str, Any] | None = None,
        llm_client: Any | None = None,
        enable_neo4j_mapping: bool | None = None,
        use_llm_adjudication: bool | None = None,
        review_io: bool = False,
    ) -> "PipelineDeps":
        """Construct deps from app config + optional LLM client.

        Neo4j / embedding setup is best-effort; mapping falls back to
        CREATE_NEW when candidates are unavailable.
        """
        if ontology is None:
            raise RuntimeError(
                "PipelineDeps requires the authoritative runtime ontology; "
                "configure IngestionRunContext before building pipeline dependencies"
            )
        mapping_enabled = (
            enable_neo4j_mapping
            if enable_neo4j_mapping is not None
            else _config_bool("ingestion.entity_mapping.enabled", True)
        )
        # Mapping/reconciliation ambiguity is statistical and shared by nodes
        # and relationship endpoints. LLM remains in extraction, not this gate.
        llm_adj = False

        llm_callable = None
        if llm_client is not None:
            try:
                llm_callable = make_llm_callable_from_client(llm_client)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pipeline.deps.llm_callable_failed", error=str(exc))

        # Classification and extraction must share one merged ontology contract.
        # Older callers without the canonical model and policy retain their input.
        runtime_ontology = ontology
        if canonical_model is not None and extraction_policy is not None:
            runtime_ontology = build_lane2_ontology(
                customer_ontology=ontology,
                canonical_model=canonical_model,
                extraction_policy=extraction_policy,
            )

        prompt_provider = None
        try:
            prompt_provider = YamlOntologyProvider(
                runtime_ontology,
                run_scope=run_scope,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("pipeline.deps.prompt_provider_failed", error=str(exc))

        candidate_reader = None
        semantic = None
        reranker = None
        context_evidence_reader = None
        if mapping_enabled:
            candidate_reader, semantic = _try_build_mapping_backends(
                llm_client,
                customer_id=(run_scope or {}).get("customer_id"),
                tenant_id=(run_scope or {}).get("tenant_id"),
                product_line_id=(run_scope or {}).get("product_line_id"),
            )
            reranker = _try_build_reranker()
            context_evidence_reader = _try_build_weaviate_context_reader()

        return cls(
            ontology=runtime_ontology,
            run_scope=dict(run_scope or {}),
            llm_callable=llm_callable,
            prompt_provider=prompt_provider,
            candidate_reader=candidate_reader,
            semantic=semantic,
            reranker=reranker,
            context_evidence_reader=context_evidence_reader,
            mapping_enabled=mapping_enabled,
            use_llm_adjudication=llm_adj,
            review_io=review_io,
        )

    @classmethod
    def from_clients(
        cls,
        *,
        llm_client: Any | None = None,
        ontology: dict[str, Any] | None = None,
        canonical_model: dict[str, Any] | None = None,
        extraction_policy: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> "PipelineDeps":
        """Alias for ``from_config`` (preferred name for external callers)."""
        return cls.from_config(
            llm_client=llm_client,
            ontology=ontology,
            canonical_model=canonical_model,
            extraction_policy=extraction_policy,
            **kwargs,
        )


class PipelineBlockResult(BaseModel):
    """Stable output of a reusable block-level pipeline run."""

    lane: str
    block_id: str
    source_file: str | None = None
    classification: ClassificationResult | None = None
    extractions: list[ExtractionResult] = Field(default_factory=list)
    extraction: ExtractionResult | None = None
    two_pass: Lane2TwoPassResult | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    mapping_results: list[EntityMappingResult] = Field(default_factory=list)
    ie_dict: dict[str, Any] = Field(default_factory=dict)
    success: bool = False
    failure_reason: str | None = None
    steps_run: list[str] = Field(default_factory=list)


def _config_bool(dotted: str, default: bool) -> bool:
    try:
        cfg = get_app_config()
        cur: Any = cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        if isinstance(cur, bool):
            return cur
        return str(cur).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _try_build_mapping_backends(
    llm_client: Any | None, *, customer_id: str | None = None,
    tenant_id: str | None = None, product_line_id: str | None = None,
) -> tuple[Any | None, Any | None]:
    try:
        from kbfabric_ingest.ingest.lane2_node_reconciliation import (
            Neo4jCandidateReader,
            RealEmbeddingProvider,
            MockSemanticSimilarityProvider,
            make_neo4j_driver_from_config,
        )

        reader = None
        try:
            driver = make_neo4j_driver_from_config(get_app_config())
            reader = Neo4jCandidateReader(
                driver, customer_id=customer_id, tenant_id=tenant_id,
                product_line_id=product_line_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("pipeline.deps.neo4j_unavailable", error=str(exc))

        semantic: Any
        if llm_client is not None and hasattr(llm_client, "embed_text"):

            def _embed_fn(texts: list[str]) -> list[list[float]]:
                return llm_client.embed_text(texts)

            semantic = RealEmbeddingProvider(_embed_fn)
        else:
            semantic = MockSemanticSimilarityProvider()
        return reader, semantic
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline.deps.mapping_backends_failed", error=str(exc))
        return None, None


def _try_build_reranker() -> Any | None:
    """Construct the bundled offline cross-encoder when its local files exist."""
    try:
        from pathlib import Path
        from custom_ml.reranking.reranker_client import RerankerClient

        model_dir = Path(__file__).resolve().parents[3] / "custom_ml/reranking/models/mmarco-mMiniLMv2-L12-H384-v1"
        if not model_dir.exists():
            logger.info("pipeline.deps.reranker_unavailable", reason="local_model_missing")
            return None
        return RerankerClient({"reranking": {"local_model_path": str(model_dir)}})
    except Exception as exc:  # noqa: BLE001
        logger.info("pipeline.deps.reranker_unavailable", error=str(exc))
        return None


def _try_build_weaviate_context_reader() -> Any | None:
    """Build a lazy read-only helper; it opens Weaviate only during an evidence read."""
    try:
        from kbfabric_ingest.ingest.context_evidence import WeaviateContextReader
        cfg = get_app_config().get("weaviate", {}) or {}
        if not cfg.get("url"):
            return None
        return WeaviateContextReader(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.info("pipeline.deps.weaviate_context_unavailable", error=str(exc))
        return None

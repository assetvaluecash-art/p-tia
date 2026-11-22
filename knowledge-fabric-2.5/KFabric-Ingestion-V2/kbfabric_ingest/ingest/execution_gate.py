"""Deterministic execution gate between mapping/context resolution and KG writes."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.relationship_endpoint_resolution import ContextResolutionResult
from kbfabric_ingest.ingest.entity_mapping import EntityMappingResult, MappingDecision
from kbfabric_ingest.ingest.config import get_app_config
from kbfabric_ingest.utils.logger import get_logger


logger = get_logger(__name__)


class HITLMode(str, Enum):
    AUTO_WRITE = "auto_write"
    REVIEW_AMBIGUOUS = "review_ambiguous"
    REVIEW_ALL = "review_all"


class ExecutionOutcome(str, Enum):
    REJECTED = "REJECTED"
    UNRESOLVED_WRITE = "UNRESOLVED_WRITE"
    WRITE_READY = "WRITE_READY"
    HITL_REQUIRED = "HITL_REQUIRED"


def resolve_hitl_mode(requested: HITLMode | str | None = None) -> HITLMode:
    """Resolve a validated per-run policy, falling back to service config."""
    if requested is not None:
        return requested if isinstance(requested, HITLMode) else HITLMode(requested)
    return get_configured_hitl_mode()


def get_configured_hitl_mode() -> HITLMode:
    """Return the effective HITL mode from application configuration."""
    config = get_app_config()
    raw_mode = config.get("ingestion", {}).get(
        "hitl_mode",
        HITLMode.REVIEW_AMBIGUOUS.value,
    )
    mode = HITLMode(raw_mode)
    logger.info(
        "execution_gate.hitl_mode.configured",
        log_kind="output",
        hitl_mode=mode.value,
    )
    return mode


class ExecutionGateResult(BaseModel):
    outcome: ExecutionOutcome
    mapping_decision: str
    reason_code: str
    justification: str
    selected_entity_id: str | None = None
    source_temporary_id: str | None = None
    context_resolutions: list[ContextResolutionResult] = Field(
        default_factory=list
    )
    provenance: dict[str, Any] = Field(default_factory=dict)


def evaluate_execution_gate(
    mapping: EntityMappingResult,
    *,
    hitl_mode: HITLMode = HITLMode.REVIEW_AMBIGUOUS,
) -> ExecutionGateResult:
    """Evaluate a mapping without performing any external write."""

    def result(
        outcome: ExecutionOutcome,
        reason_code: str,
        justification: str,
        *,
        mapping_decision: str | None = None,
        selected_entity_id: str | None = None,
        context_resolutions: list[ContextResolutionResult] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ExecutionGateResult:
        value = ExecutionGateResult(
            outcome=outcome,
            mapping_decision=mapping_decision or mapping.decision.value,
            reason_code=reason_code,
            justification=justification,
            selected_entity_id=(
                selected_entity_id
                if selected_entity_id is not None
                else mapping.selected_entity_id
            ),
            source_temporary_id=mapping.incoming.temporary_id,
            context_resolutions=(
                mapping.context_resolutions
                if context_resolutions is None
                else context_resolutions
            ),
            provenance=mapping.provenance if provenance is None else provenance,
        )

        logger.info(
            "execution_gate.evaluated",
            log_kind="output",
            outcome=outcome.value,
            mapping_decision=mapping.decision.value,
            reason_code=reason_code,
            block_id=mapping.incoming.block_id,
        )
        return value

    # Ontology-invalid relationships are never eligible for HITL.
    # This check must precede all review-policy checks so invalid ontology
    # decisions can never be rescued by human approval.
    invalid_relationships = [
        r
        for r in mapping.context_resolutions
        if r.decision == "REJECTED"
        and r.reason_code == "invalid_relationship_triple"
    ]

    if invalid_relationships:
        return result(
            ExecutionOutcome.REJECTED,
            "invalid_relationship_triple",
            (
                "At least one extracted relationship violates the compiled "
                "ontology relationship contract."
            ),
        )

    def auto_write_resolutions() -> tuple[list[ContextResolutionResult], list[str]]:
        resolutions = []
        selected_ids = []
        for resolution in mapping.context_resolutions:
            if resolution.decision != "RELATIONSHIP_REVIEW_REQUIRED":
                resolutions.append(resolution)
                continue
            candidate = next(
                (
                    item
                    for item in resolution.candidates
                    if item.entity_id and item.node_type == resolution.target_type
                ),
                None,
            )
            if candidate is None:
                resolutions.append(resolution)
                continue
            selected_ids.append(candidate.entity_id)
            resolutions.append(
                resolution.model_copy(
                    update={
                        "decision": "RESOLVED",
                        "selected_entity_id": candidate.entity_id,
                        "reason_code": "auto_write_top_ranked_candidate",
                        "justification": (
                            f"Auto-write selected the highest-ranked same-type "
                            f"candidate {candidate.name!r} (rank {candidate.rank}, "
                            f"fusion score {candidate.fusion_score:.4f}); original "
                            "candidate evidence is retained."
                        ),
                        "provenance": {
                            **resolution.provenance,
                            "auto_write_decision": {
                                "strategy": "top_ranked_same_type_candidate",
                                "selected_entity_id": candidate.entity_id,
                                "selected_entity_name": candidate.name,
                                "selected_entity_type": candidate.node_type,
                                "candidate_rank": candidate.rank,
                                "fusion_score": candidate.fusion_score,
                                "candidate_count": len(resolution.candidates),
                            },
                        },
                    }
                )
            )
        return resolutions, selected_ids

    # AUTO_WRITE is an explicit opt-in to deterministic, top-ranked candidate
    # selection. The candidate reader has already applied tenant/customer,
    # domain, product-line, and node-type constraints. Preserve the original
    # uncertainty and selected evidence in provenance for later audit.
    if mapping.decision == MappingDecision.REQUIRES_REVIEW:
        if hitl_mode == HITLMode.AUTO_WRITE:
            top_candidate = next(
                (
                    candidate
                    for candidate in mapping.candidates
                    if candidate.candidate.entity_id
                    and candidate.candidate.kg_node_type == mapping.incoming.node_type
                ),
                None,
            )
            if top_candidate is not None:
                resolutions, relationship_ids = auto_write_resolutions()
                auto_provenance = dict(mapping.provenance)
                auto_provenance["auto_write_decision"] = {
                    "strategy": "top_ranked_same_type_candidate",
                    "original_mapping_decision": mapping.decision.value,
                    "selected_entity_id": top_candidate.candidate.entity_id,
                    "selected_entity_name": top_candidate.candidate.name,
                    "selected_entity_type": top_candidate.candidate.kg_node_type,
                    "candidate_rank": top_candidate.rank,
                    "fusion_score": top_candidate.fusion_score,
                    "candidate_count": len(mapping.candidates),
                    "relationship_candidate_ids": relationship_ids,
                }
                if any(
                    r.decision == "RELATIONSHIP_REVIEW_REQUIRED"
                    for r in resolutions
                ):
                    return result(
                        ExecutionOutcome.HITL_REQUIRED,
                        "auto_write_relationship_candidate_not_eligible",
                        "The primary entity has an eligible automatic match, but at least one relationship has no eligible same-type candidate. The plan remains in review to avoid silently dropping or misdirecting that relationship.",
                        selected_entity_id=top_candidate.candidate.entity_id,
                        context_resolutions=resolutions,
                        provenance=auto_provenance,
                    )
                return result(
                    ExecutionOutcome.WRITE_READY,
                    "auto_write_top_ranked_entity_candidate",
                    (
                        "The auto_write policy selected the highest-ranked "
                        "same-type candidate. The original ambiguous mapping "
                        "and candidate evidence are retained for audit."
                    ),
                    mapping_decision=MappingDecision.REUSE_EXISTING.value,
                    selected_entity_id=top_candidate.candidate.entity_id,
                    context_resolutions=resolutions,
                    provenance=auto_provenance,
                )

        # REVIEW_AMBIGUOUS and REVIEW_ALL keep uncertain entity identity in
        # HITL. AUTO_WRITE also fails closed when no eligible same-type
        # candidate exists to select deterministically.
        return result(
            ExecutionOutcome.HITL_REQUIRED,
            "primary_entity_review_required",
            (
                "The primary entity identity requires review, so no KG write "
                "is permitted until that identity decision is approved."
            ),
        )

    ambiguous = [
        r
        for r in mapping.context_resolutions
        if r.decision == "RELATIONSHIP_REVIEW_REQUIRED"
    ]

    if ambiguous and hitl_mode == HITLMode.AUTO_WRITE:
        resolutions, auto_decisions = auto_write_resolutions()
        remaining_ambiguous = [
            r for r in resolutions
            if r.decision == "RELATIONSHIP_REVIEW_REQUIRED"
        ]
        if auto_decisions:
            auto_provenance = {
                **mapping.provenance,
                "auto_write_relationship_decisions": auto_decisions,
            }
            if not remaining_ambiguous:
                return result(
                    ExecutionOutcome.WRITE_READY,
                    "auto_write_top_ranked_relationship_candidates",
                    "The auto_write policy selected the highest-ranked same-type relationship candidates; original candidate evidence is retained for audit.",
                    context_resolutions=resolutions,
                    provenance=auto_provenance,
                )
            return result(
                ExecutionOutcome.HITL_REQUIRED,
                "auto_write_relationship_candidate_not_eligible",
                "At least one relationship has no eligible same-type candidate for automatic selection. The plan remains in review to avoid silently dropping or misdirecting that relationship.",
                context_resolutions=resolutions,
                provenance=auto_provenance,
            )
        if remaining_ambiguous:
            return result(
                ExecutionOutcome.HITL_REQUIRED,
                "auto_write_relationship_candidate_not_eligible",
                "At least one relationship has no eligible same-type candidate for automatic selection. The plan remains in review to avoid silently dropping or misdirecting that relationship.",
                context_resolutions=resolutions,
            )

    if ambiguous and hitl_mode != HITLMode.AUTO_WRITE:
        return result(
            ExecutionOutcome.HITL_REQUIRED,
            "relationship_context_review_required",
            (
                "One or more relationship endpoints have ambiguous or "
                "insufficient context and require human confirmation."
            ),
        )

    unresolved = [
        r
        for r in mapping.context_resolutions
        if r.decision == "UNRESOLVED_REFERENCE"
    ]

    if unresolved and hitl_mode != HITLMode.REVIEW_ALL:
        return result(
            ExecutionOutcome.UNRESOLVED_WRITE,
            "unresolved_reference",
            (
                "The relationship is ontology-valid but no eligible target "
                "candidate was resolved. The unresolved reference may be "
                "written and resolved when relevant context arrives later."
            ),
        )

    if hitl_mode == HITLMode.REVIEW_ALL:
        return result(
            ExecutionOutcome.HITL_REQUIRED,
            "customer_review_all_policy",
            (
                "The customer policy requires human approval before an "
                "otherwise valid KG write."
            ),
        )

    return result(
        ExecutionOutcome.WRITE_READY,
        "valid_write",
        (
            "The mapping is ontology-valid and eligible for KG writing "
            "under the current HITL policy."
        ),
    )


__all__ = [
    "ExecutionGateResult",
    "ExecutionOutcome",
    "HITLMode",
    "evaluate_execution_gate",
    "resolve_hitl_mode",
]

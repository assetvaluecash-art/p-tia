"""Ontology-wide admission and enforcement engine.

This module is deliberately deterministic.

Responsibilities:
    1. Validate entity types against the authoritative ontology.
    2. Validate relationship types against the authoritative ontology.
    3. Validate relationship endpoint types against ontology constraints.
    4. Apply ontology-configured evidence gates when present.
    5. Ensure relationships cannot survive when either endpoint was rejected.
    6. Produce structured admission diagnostics for audit/metrics.
    7. Remain independent of LLM/LiteLLM/Langfuse implementations.

Important architecture rule:
    LLMs propose.
    OntologyAdmissionEngine disposes.

The engine does not call an LLM and does not create reasoning traces.
The surrounding ingestion/LLM layer remains responsible for LiteLLM,
OpenTelemetry and Langfuse instrumentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from kbfabric_ingest.utils.logger import get_logger


DEFAULT_ENTITY_CONFIDENCE_THRESHOLD = 0.70
DEFAULT_RELATIONSHIP_CONFIDENCE_THRESHOLD = 0.70

# Generic evidence-gate keyword categories (node-type agnostic).
_EVIDENCE_KEYWORD_CATEGORIES = (
    "step_keywords",
    "actor_keywords",
    "system_keywords",
    "action_keywords",
)

logger = get_logger(__name__)


def compile_relationship_triples(
    relationship_types: dict[str, dict[str, Any]],
) -> set[tuple[str, str, str]]:
    """Compile ontology relationship definitions into exact endpoint triples.

    This is the shared executable interpretation of the YAML relationship
    contract. Every downstream consumer must use these exact triples rather
    than independently combining relationship names and endpoint lists.
    """
    triples: set[tuple[str, str, str]] = set()

    for relationship_name, definition in relationship_types.items():
        if not isinstance(definition, dict):
            continue

        allowed_triples = definition.get("allowed_triples")

        if isinstance(allowed_triples, list):
            for triple in allowed_triples:
                if not isinstance(triple, dict):
                    continue

                from_type = str(triple.get("from", "")).strip()
                to_type = str(triple.get("to", "")).strip()

                if from_type and to_type:
                    triples.add((from_type, relationship_name, to_type))
            continue

        from_types = definition.get("from", [])
        to_types = definition.get("to", [])

        if isinstance(from_types, str):
            from_types = [from_types]
        if isinstance(to_types, str):
            to_types = [to_types]

        for from_type, to_type in zip(from_types, to_types):
            from_type = str(from_type).strip()
            to_type = str(to_type).strip()

            if from_type and to_type:
                triples.add((from_type, relationship_name, to_type))

    return triples



class AdmissionDecision(Enum):
    """Public trust decision for an individual candidate."""

    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass(frozen=True)
class _EntityAdmissionDecision:
    """Internal deterministic entity decision used by batch admission."""

    admitted: bool
    reason: str
    entity: dict[str, Any]


@dataclass(frozen=True)
class _RelationshipAdmissionDecision:
    """Internal deterministic relationship decision used by batch admission."""

    admitted: bool
    reason: str
    relationship: dict[str, Any]


@dataclass(frozen=True)
class CandidateAdmissionResult:
    """Public result for one entity or relationship candidate."""

    decision: AdmissionDecision
    candidate_type: str | None
    reasons: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    candidate: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdmissionReport:
    """Complete deterministic admission result for one source block."""

    admitted_entities: list[dict[str, Any]] = field(default_factory=list)
    rejected_entities: list[dict[str, Any]] = field(default_factory=list)

    admitted_relationships: list[dict[str, Any]] = field(default_factory=list)
    rejected_relationships: list[dict[str, Any]] = field(default_factory=list)

    entity_rejection_reasons: dict[str, int] = field(default_factory=dict)
    relationship_rejection_reasons: dict[str, int] = field(default_factory=dict)

    def count_entity_rejection(self, reason: str) -> None:
        self.entity_rejection_reasons[reason] = (
            self.entity_rejection_reasons.get(reason, 0) + 1
        )

    def count_relationship_rejection(self, reason: str) -> None:
        self.relationship_rejection_reasons[reason] = (
            self.relationship_rejection_reasons.get(reason, 0) + 1
        )


class OntologyAdmissionEngine:
    """Authoritative ontology-wide entity/relationship admission engine.

    The ontology is the source of truth.

    No node type is admitted merely because an LLM proposed it.
    No relationship is admitted merely because its relation name exists.
    Endpoint compatibility must also be satisfied.
    """

    def __init__(
        self,
        ontology: dict[str, Any],
        *,
        entity_confidence_threshold: float = DEFAULT_ENTITY_CONFIDENCE_THRESHOLD,
        relationship_confidence_threshold: float = DEFAULT_RELATIONSHIP_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.ontology = ontology or {}

        self.entity_confidence_threshold = entity_confidence_threshold
        self.relationship_confidence_threshold = relationship_confidence_threshold

        self.node_types: dict[str, dict[str, Any]] = {
            str(item["name"]): item
            for item in self.ontology.get("node_types", [])
            if isinstance(item, dict) and item.get("name")
        }

        self.relationship_types: dict[str, dict[str, Any]] = {
            str(item["name"]): item
            for item in self.ontology.get("relationship_types", [])
            if isinstance(item, dict) and item.get("name")
        }

        self.allowed_relationship_triples = self._compile_relationship_triples()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def admit_entity(
        self,
        entity: dict[str, Any],
        *,
        source_text: str = "",
        block_category: str | None = None,
    ) -> CandidateAdmissionResult:
        """Evaluate one entity candidate against the generic admission contract.

        This public API intentionally exposes ACCEPT / REVIEW / REJECT.
        REVIEW candidates are never included in the trusted KG by the batch
        ``admit`` path.
        """
        if not isinstance(entity, dict):
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=None,
                reasons=["Candidate is not a mapping."],
                reason_codes=["entity_not_mapping"],
                candidate={},
            )

        entity_type = str(entity.get("type", "")).strip() or None

        if not entity_type:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=None,
                reasons=["Entity type is required."],
                reason_codes=["missing_node_type"],
                candidate=entity.copy(),
            )

        # Domain hierarchy is deterministic spine data, not ordinary LLM
        # admission. Narrative is content, not a KG node.
        node_definition = self.node_types.get(entity_type)

        if node_definition is not None:
            layer = str(node_definition.get("layer", "")).strip().lower()

            if layer == "domain_structure":
                return CandidateAdmissionResult(
                    decision=AdmissionDecision.REJECT,
                    candidate_type=entity_type,
                    reasons=[
                        "Domain structure nodes are seeded deterministically."
                    ],
                    reason_codes=["domain_structure_not_llm_admissible"],
                    candidate=entity.copy(),
                )

            if layer == "narrative":
                return CandidateAdmissionResult(
                    decision=AdmissionDecision.REJECT,
                    candidate_type=entity_type,
                    reasons=["Narrative content is not a KG node."],
                    reason_codes=["non_kg_node_layer"],
                    candidate=entity.copy(),
                )

        if entity_type not in self.node_types:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=entity_type,
                reasons=["Node type is not present in the authoritative ontology."],
                reason_codes=["unknown_node_type"],
                candidate=entity.copy(),
            )

        value = str(entity.get("value", "")).strip()
        if not value:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=entity_type,
                reasons=["Entity value is required."],
                reason_codes=["missing_entity_value"],
                candidate=entity.copy(),
            )

        confidence = self._confidence(
            entity.get("confidence"),
            default=1.0,
        )

        if confidence < self.entity_confidence_threshold:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REVIEW,
                candidate_type=entity_type,
                reasons=["Entity confidence is below the automatic admission threshold."],
                reason_codes=["low_confidence"],
                candidate=entity.copy(),
            )

        evidence = str(entity.get("evidence", "") or "").strip()
        source_block_ids = entity.get("source_block_ids")

        if not evidence and not source_text.strip():
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REVIEW,
                candidate_type=entity_type,
                reasons=["Candidate lacks sufficient provenance/evidence."],
                reason_codes=["insufficient_evidence"],
                candidate=entity.copy(),
            )

        internal = self._admit_entity(
            entity,
            source_text=source_text,
            block_category=block_category,
        )

        if internal.admitted:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.ACCEPT,
                candidate_type=entity_type,
                reasons=[],
                reason_codes=[],
                candidate=internal.entity,
            )

        return CandidateAdmissionResult(
            decision=AdmissionDecision.REJECT,
            candidate_type=entity_type,
            reasons=[internal.reason],
            reason_codes=[internal.reason],
            candidate=internal.entity,
        )

    def admit_relationship(
        self,
        relationship: dict[str, Any],
        *,
        source_text: str = "",
    ) -> CandidateAdmissionResult:
        """Evaluate one relationship against ontology vocabulary and endpoints.

        Endpoint *existence* is intentionally not checked here because this
        API evaluates one relationship in isolation. Batch ``admit`` performs
        the stronger endpoint-existence check after entity admission.
        """
        if not isinstance(relationship, dict):
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=None,
                reasons=["Relationship is not a mapping."],
                reason_codes=["relationship_not_mapping"],
                candidate={},
            )

        rel_type = str(
            relationship.get("type")
            or relationship.get("rel_type")
            or relationship.get("relationship_type")
            or ""
        ).strip()

        from_type = str(relationship.get("from_type", "")).strip()
        from_value = str(relationship.get("from_value", "")).strip()
        to_type = str(relationship.get("to_type", "")).strip()
        to_value = str(relationship.get("to_value", "")).strip()

        if not rel_type:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=None,
                reasons=["Relationship type is required."],
                reason_codes=["missing_relationship_type"],
                candidate=relationship.copy(),
            )

        if not from_type or not from_value or not to_type or not to_value:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=rel_type,
                reasons=["Both relationship endpoints require type and value."],
                reason_codes=["missing_relationship_endpoint"],
                candidate=relationship.copy(),
            )

        if rel_type not in self.relationship_types:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=rel_type,
                reasons=["Relationship type is not present in the authoritative ontology."],
                reason_codes=["unknown_relationship_type"],
                candidate=relationship.copy(),
            )

        definition = self.relationship_types[rel_type]

        relationship_triple = (
            from_type,
            rel_type,
            to_type,
        )

        if relationship_triple not in self.allowed_relationship_triples:
            logger.info(
                "Ontology relationship rejected: invalid triple %s -[%s]-> %s",
                from_type,
                rel_type,
                to_type,
            )
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=rel_type,
                reasons=[
                    f"Relationship triple is not allowed by the authoritative ontology: "
                    f"{from_type} -[{rel_type}]-> {to_type}."
                ],
                reason_codes=["invalid_relationship_triple"],
                candidate=relationship.copy(),
            )

        if from_type not in self.node_types:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=rel_type,
                reasons=["Relationship source node type is unknown."],
                reason_codes=["invalid_relationship_source_type"],
                candidate=relationship.copy(),
            )

        if to_type not in self.node_types:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REJECT,
                candidate_type=rel_type,
                reasons=["Relationship target node type is unknown."],
                reason_codes=["invalid_relationship_target_type"],
                candidate=relationship.copy(),
            )

        # Exact (source type, relationship type, target type) admission above
        # is the authoritative ontology contract. Endpoint membership is already
        # covered by the compiled triple contract and node-type checks.

        confidence = self._confidence(
            relationship.get("confidence"),
            default=1.0,
        )

        if confidence < self.relationship_confidence_threshold:
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REVIEW,
                candidate_type=rel_type,
                reasons=["Relationship confidence is below the automatic admission threshold."],
                reason_codes=["low_confidence"],
                candidate=relationship.copy(),
            )

        evidence = str(relationship.get("evidence", "") or "").strip()
        source_block_ids = relationship.get("source_block_ids")

        if not evidence and not source_block_ids and not source_text.strip():
            return CandidateAdmissionResult(
                decision=AdmissionDecision.REVIEW,
                candidate_type=rel_type,
                reasons=["Relationship lacks sufficient provenance/evidence."],
                reason_codes=["insufficient_evidence"],
                candidate=relationship.copy(),
            )

        logger.info(
            "Ontology relationship admitted: %s -[%s]-> %s",
            from_type,
            rel_type,
            to_type,
        )

        return CandidateAdmissionResult(
            decision=AdmissionDecision.ACCEPT,
            candidate_type=rel_type,
            reasons=[],
            reason_codes=[],
            candidate=relationship.copy(),
        )

    def admit(
        self,
        entities: list[dict[str, Any]] | None,
        relationships: list[dict[str, Any]] | None,
        *,
        source_text: str = "",
        block_category: str | None = None,
    ) -> AdmissionReport:
        """Admit a complete entity/relationship candidate set.

        Admission order:

            candidates
                ↓
            entity ontology validation
                ↓
            entity confidence validation
                ↓
            entity evidence gate
                ↓
            relationship ontology validation
                ↓
            endpoint existence validation
                ↓
            relationship confidence validation
                ↓
            relationship evidence gate
                ↓
            admitted result
        """

        report = AdmissionReport()

        # Phase 1: type / confidence / generic evidence gate (incl. exclusions).
        # Process structural extras run in phase 2 so Actor/System co-entities
        # from the same batch can satisfy structural signals.
        provisional_entities: list[dict[str, Any]] = []

        for entity in entities or []:
            decision = self._admit_entity(
                entity,
                source_text=source_text,
                block_category=block_category,
                apply_process_structural=False,
            )

            if decision.admitted:
                provisional_entities.append(decision.entity)
            else:
                report.rejected_entities.append(decision.entity)
                report.count_entity_rejection(decision.reason)

        admitted_entities: list[dict[str, Any]] = []

        for entity in provisional_entities:
            entity_type = str(entity.get("type", "")).strip()
            node_definition = self.node_types.get(entity_type, {})
            gate = (
                node_definition.get("evidence_gate")
                if isinstance(node_definition, dict)
                else None
            )

            if (
                entity_type == "Process"
                and isinstance(gate, dict)
                and gate.get("enabled")
            ):
                structural_reason = self._process_structural_gate(
                    gate,
                    source_text,
                    entity_value=str(entity.get("value", "") or ""),
                    batch_entities=provisional_entities,
                )
                if structural_reason is not None:
                    report.rejected_entities.append(entity)
                    report.count_entity_rejection(structural_reason)
                    continue

            admitted_entities.append(entity)

        report.admitted_entities = admitted_entities

        # Relationship admission happens only after entity admission.
        # Therefore an LLM cannot manufacture a relationship whose endpoint
        # was rejected.
        admitted_entity_keys = {
            self._entity_key(entity)
            for entity in admitted_entities
        }

        for relationship in relationships or []:
            decision = self._admit_relationship(
                relationship,
                admitted_entity_keys=admitted_entity_keys,
                source_text=source_text,
            )

            if decision.admitted:
                report.admitted_relationships.append(decision.relationship)
            else:
                report.rejected_relationships.append(decision.relationship)
                report.count_relationship_rejection(decision.reason)

        return report

    # ------------------------------------------------------------------
    # Entity admission
    # ------------------------------------------------------------------

    def _admit_entity(
        self,
        entity: dict[str, Any],
        *,
        source_text: str,
        block_category: str | None,
        batch_entities: list[dict[str, Any]] | None = None,
        apply_process_structural: bool = True,
    ) -> _EntityAdmissionDecision:
        if not isinstance(entity, dict):
            return _EntityAdmissionDecision(False, "entity_not_mapping", {})

        entity_copy = entity.copy()

        entity_type = str(entity_copy.get("type", "")).strip()
        entity_value = str(entity_copy.get("value", "")).strip()

        if not entity_type:
            return _EntityAdmissionDecision(
                False,
                "missing_entity_type",
                entity_copy,
            )

        if not entity_value:
            return _EntityAdmissionDecision(
                False,
                "missing_entity_value",
                entity_copy,
            )

        if entity_type not in self.node_types:
            return _EntityAdmissionDecision(
                False,
                "unknown_node_type",
                entity_copy,
            )

        confidence = self._confidence(
            entity_copy.get("confidence"),
            default=1.0,
        )

        if confidence < self.entity_confidence_threshold:
            return _EntityAdmissionDecision(
                False,
                "entity_confidence_below_threshold",
                entity_copy,
            )

        node_definition = self.node_types[entity_type]

        gate_reason = self._evidence_gate_rejection(
            node_definition,
            source_text,
            entity_value=entity_value,
            entity_type=entity_type,
        )
        if gate_reason is not None:
            return _EntityAdmissionDecision(
                False,
                gate_reason,
                entity_copy,
            )

        gate = node_definition.get("evidence_gate")
        if (
            apply_process_structural
            and entity_type == "Process"
            and isinstance(gate, dict)
            and gate.get("enabled")
        ):
            structural_reason = self._process_structural_gate(
                gate,
                source_text,
                entity_value=entity_value,
                batch_entities=batch_entities,
            )
            if structural_reason is not None:
                return _EntityAdmissionDecision(
                    False,
                    structural_reason,
                    entity_copy,
                )

        # Preserve admission metadata without changing the semantic payload.
        entity_copy.setdefault("_admission", {})
        entity_copy["_admission"] = {
            **entity_copy.get("_admission", {}),
            "admitted": True,
            "ontology_node_type": entity_type,
            "block_category": block_category,
        }

        return _EntityAdmissionDecision(True, "admitted", entity_copy)

    # ------------------------------------------------------------------
    # Relationship admission
    # ------------------------------------------------------------------

    def _admit_relationship(
        self,
        relationship: dict[str, Any],
        *,
        admitted_entity_keys: set[tuple[str, str]],
        source_text: str,
    ) -> _RelationshipAdmissionDecision:
        if not isinstance(relationship, dict):
            return _RelationshipAdmissionDecision(
                False,
                "relationship_not_mapping",
                {},
            )

        relationship_copy = relationship.copy()

        rel_type = str(
            relationship_copy.get("rel_type")
            or relationship_copy.get("relationship_type")
            or ""
        ).strip()

        from_type = str(relationship_copy.get("from_type", "")).strip()
        from_value = str(relationship_copy.get("from_value", "")).strip()
        to_type = str(relationship_copy.get("to_type", "")).strip()
        to_value = str(relationship_copy.get("to_value", "")).strip()

        if not rel_type:
            return _RelationshipAdmissionDecision(
                False,
                "missing_relationship_type",
                relationship_copy,
            )

        if rel_type not in self.relationship_types:
            return _RelationshipAdmissionDecision(
                False,
                "unknown_relationship_type",
                relationship_copy,
            )

        if not from_type or not from_value:
            return _RelationshipAdmissionDecision(
                False,
                "missing_from_endpoint",
                relationship_copy,
            )

        if not to_type or not to_value:
            return _RelationshipAdmissionDecision(
                False,
                "missing_to_endpoint",
                relationship_copy,
            )

        if from_type not in self.node_types:
            return _RelationshipAdmissionDecision(
                False,
                "unknown_from_node_type",
                relationship_copy,
            )

        if to_type not in self.node_types:
            return _RelationshipAdmissionDecision(
                False,
                "unknown_to_node_type",
                relationship_copy,
            )

        confidence = self._confidence(
            relationship_copy.get("confidence"),
            default=1.0,
        )

        if confidence < self.relationship_confidence_threshold:
            return _RelationshipAdmissionDecision(
                False,
                "relationship_confidence_below_threshold",
                relationship_copy,
            )

        relationship_definition = self.relationship_types[rel_type]

        if not self._endpoint_allowed(
            relationship_definition.get("from"),
            from_type,
        ):
            return _RelationshipAdmissionDecision(
                False,
                "from_endpoint_type_not_allowed",
                relationship_copy,
            )

        if not self._endpoint_allowed(
            relationship_definition.get("to"),
            to_type,
        ):
            return _RelationshipAdmissionDecision(
                False,
                "to_endpoint_type_not_allowed",
                relationship_copy,
            )

        if (from_type, from_value) not in admitted_entity_keys:
            return _RelationshipAdmissionDecision(
                False,
                "from_endpoint_not_admitted",
                relationship_copy,
            )

        if (to_type, to_value) not in admitted_entity_keys:
            return _RelationshipAdmissionDecision(
                False,
                "to_endpoint_not_admitted",
                relationship_copy,
            )

        # Relationship-level evidence gates are supported if introduced
        # into the ontology. They are optional and therefore do not alter
        # current ontology behaviour.
        if not self._passes_evidence_gate(
            relationship_definition,
            source_text,
        ):
            return _RelationshipAdmissionDecision(
                False,
                "relationship_evidence_gate_failed",
                relationship_copy,
            )

        relationship_copy.setdefault("_admission", {})
        relationship_copy["_admission"] = {
            **relationship_copy.get("_admission", {}),
            "admitted": True,
            "ontology_relationship_type": rel_type,
        }

        return _RelationshipAdmissionDecision(
            True,
            "admitted",
            relationship_copy,
        )

    # ------------------------------------------------------------------
    # Evidence gate
    # ------------------------------------------------------------------

    def _passes_evidence_gate(
        self,
        ontology_definition: dict[str, Any],
        source_text: str,
        *,
        entity_value: str = "",
        entity_type: str = "",
    ) -> bool:
        """Return True when the optional ontology evidence gate passes."""
        return (
            self._evidence_gate_rejection(
                ontology_definition,
                source_text,
                entity_value=entity_value,
                entity_type=entity_type,
            )
            is None
        )

    def _evidence_gate_rejection(
        self,
        ontology_definition: dict[str, Any],
        source_text: str,
        *,
        entity_value: str = "",
        entity_type: str = "",
    ) -> str | None:
        """Apply an optional ontology-defined evidence gate.

        Existing Process evidence_gate configuration continues to work,
        but the keyword scoring is intentionally node-type agnostic.

        Supported configuration:

            evidence_gate:
              enabled: true
              min_evidence_score: 1
              step_keywords: [...]
              actor_keywords: [...]
              system_keywords: [...]
              action_keywords: [...]
              exclusion_keywords: [...]   # enforced when present
              outcome_keywords: [...]     # Process structural only

        Candidate ``value`` / name is never scored. When ``entity_value`` is
        provided it is redacted from the gated text before matching so names
        cannot self-satisfy action stems such as generate / classify / book.
        """

        gate = ontology_definition.get("evidence_gate")

        if not isinstance(gate, dict) or not gate.get("enabled"):
            return None

        gated_text = self._build_gated_text(source_text, entity_value)
        text = gated_text.lower()
        group_hits = self._keyword_group_hits(gate, text)
        score = sum(1 for hit in group_hits.values() if hit)
        min_score = int(gate.get("min_evidence_score", 1))
        exclusion_matched = self._exclusion_matched(gate, text)

        audit = {
            "entity_type": entity_type or None,
            "matched_keyword_group_count": score,
            "matched_keyword_groups": [
                name for name, hit in group_hits.items() if hit
            ],
            "required_score": min_score,
            "actual_score": score,
            "exclusion_matched": exclusion_matched,
        }

        if exclusion_matched:
            logger.info(
                "ontology.admission.evidence_gate",
                decision="REJECT",
                reason_code="node_evidence_exclusion_matched",
                **audit,
            )
            return "node_evidence_exclusion_matched"

        if score < min_score:
            logger.info(
                "ontology.admission.evidence_gate",
                decision="REJECT",
                reason_code="node_evidence_gate_failed",
                **audit,
            )
            return "node_evidence_gate_failed"

        logger.info(
            "ontology.admission.evidence_gate",
            decision="ACCEPT",
            reason_code="evidence_gate_passed",
            **audit,
        )
        return None

    def _process_structural_gate(
        self,
        gate: dict[str, Any],
        source_text: str,
        *,
        entity_value: str = "",
        batch_entities: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Process-only structural signals beyond the generic keyword gate.

        Requires Actor, System, Activity, and Outcome evidence. Actor/System
        may be satisfied by keyword hits in value-redacted block text or by
        co-extracted Actor/System entities in the same admission batch.
        """

        gated_text = self._build_gated_text(source_text, entity_value)
        text = gated_text.lower()
        group_hits = self._keyword_group_hits(gate, text)

        batch_types = {
            str(item.get("type", "")).strip()
            for item in (batch_entities or [])
            if isinstance(item, dict)
        }

        actor_from_keywords = bool(group_hits.get("actor_keywords"))
        actor_from_co_entities = "Actor" in batch_types
        actor_ok = actor_from_keywords or actor_from_co_entities

        system_from_keywords = bool(group_hits.get("system_keywords"))
        system_from_co_entities = "System" in batch_types
        system_ok = system_from_keywords or system_from_co_entities

        activity_ok = bool(group_hits.get("step_keywords")) or bool(
            group_hits.get("action_keywords")
        )

        outcome_keywords = gate.get("outcome_keywords", [])
        outcome_ok = isinstance(outcome_keywords, list) and any(
            str(keyword).lower() in text for keyword in outcome_keywords
        )

        audit = {
            "matched_keyword_group_count": sum(
                1 for hit in group_hits.values() if hit
            ),
            "matched_keyword_groups": [
                name for name, hit in group_hits.items() if hit
            ],
            "required_score": int(gate.get("min_evidence_score", 1)),
            "actual_score": sum(1 for hit in group_hits.values() if hit),
            "exclusion_matched": False,
            "actor_from_keywords": actor_from_keywords,
            "actor_from_co_entities": actor_from_co_entities,
            "system_from_keywords": system_from_keywords,
            "system_from_co_entities": system_from_co_entities,
            "activity_signal": activity_ok,
            "outcome_signal": outcome_ok,
        }

        if not actor_ok:
            logger.info(
                "ontology.admission.process_structural_gate",
                decision="REJECT",
                reason_code="process_missing_actor_signal",
                **audit,
            )
            return "process_missing_actor_signal"

        if not system_ok:
            logger.info(
                "ontology.admission.process_structural_gate",
                decision="REJECT",
                reason_code="process_missing_system_signal",
                **audit,
            )
            return "process_missing_system_signal"

        if not activity_ok:
            logger.info(
                "ontology.admission.process_structural_gate",
                decision="REJECT",
                reason_code="process_missing_activity_signal",
                **audit,
            )
            return "process_missing_activity_signal"

        if not outcome_ok:
            logger.info(
                "ontology.admission.process_structural_gate",
                decision="REJECT",
                reason_code="process_missing_outcome_signal",
                **audit,
            )
            return "process_missing_outcome_signal"

        logger.info(
            "ontology.admission.process_structural_gate",
            decision="ACCEPT",
            reason_code="process_structural_passed",
            **audit,
        )
        return None

    @staticmethod
    def _build_gated_text(source_text: str, entity_value: str = "") -> str:
        """Build evidence text from block text with candidate value redacted."""
        text = source_text or ""
        value = (entity_value or "").strip()
        if not value:
            return text
        return re.sub(re.escape(value), " ", text, flags=re.IGNORECASE)

    @staticmethod
    def _keyword_group_hits(
        gate: dict[str, Any],
        text_lower: str,
    ) -> dict[str, bool]:
        hits: dict[str, bool] = {}
        for category in _EVIDENCE_KEYWORD_CATEGORIES:
            keywords = gate.get(category, [])
            hits[category] = isinstance(keywords, list) and any(
                str(keyword).lower() in text_lower for keyword in keywords
            )
        return hits

    @staticmethod
    def _exclusion_matched(gate: dict[str, Any], text_lower: str) -> bool:
        exclusions = gate.get("exclusion_keywords", [])
        if not isinstance(exclusions, list):
            return False
        return any(
            str(keyword).lower() in text_lower
            for keyword in exclusions
            if str(keyword).strip()
        )

    # ------------------------------------------------------------------
    # Ontology helpers
    # ------------------------------------------------------------------

    def _compile_relationship_triples(self) -> set[tuple[str, str, str]]:
        """Compile ontology relationship definitions into exact endpoint triples."""
        triples = compile_relationship_triples(self.relationship_types)

        logger.info(
            "Ontology admission contract compiled: node_types=%d relationship_types=%d "
            "allowed_relationship_triples=%d",
            len(self.node_types),
            len(self.relationship_types),
            len(triples),
        )

        return triples

    @staticmethod
    def _endpoint_allowed(
        configured: Any,
        actual: str,
    ) -> bool:
        if configured is None:
            return True

        if isinstance(configured, str):
            return actual == configured

        if isinstance(configured, (list, tuple, set)):
            return actual in configured

        return False

    @staticmethod
    def _confidence(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _entity_key(entity: dict[str, Any]) -> tuple[str, str]:
        return (
            str(entity.get("type", "")).strip(),
            str(entity.get("value", "")).strip(),
        )

"""Lane 2 two-pass LLM IE (classification → category-specific extraction).

Production text IE can use this path when ``ingestion.text_ie.mode`` is
``two_pass`` (see ``IEClient.enrich_blocks``). Also usable standalone via
demo/tests.

Pass 1: classify CanonicalBlock into one or more ontology block_categories.
Pass 2: extract entities/relationships once per identified category, then merge.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from kbfabric_ingest.ingest.ontology_admission import compile_relationship_triples
from kbfabric_ingest.ingest.requirement_entity_fields import extract_entity_description_from_text
from kbfabric_ingest.ingest.models import BlockCategory, CanonicalBlock, EnrichedBlock
from kbfabric_ingest.ingest.ontology_prompt import (
    CLASSIFICATION_DECISION_TREE,
    build_block_categories_section,
    get_allowed_category_names,
    get_allowed_node_types,
    get_allowed_relationship_types,
    normalize_to_ontology,
)
from kbfabric_ingest.utils.logger import get_logger
from kbfabric_ingest.utils.ingestion_observability import (
    get_observability_config,
    log_llm_payloads_to_server_logs,
    redact_for_log,
)

logger = get_logger(__name__)

# Prompt ids/versions for reviewable structured logs
PASS1_PROMPT_ID = "lane2_pass1_classify"
PASS1_PROMPT_VERSION = "v2"
PASS2_PROMPT_ID_PREFIX = "lane2_pass2_extract"
PASS2_PROMPT_VERSION = "v2"

# Categories with richer mock extraction packs (Aurthur packs not yet available)
_MOCK_PACK_CATEGORIES = frozenset({"Process", "Rule", "Requirement", "Control"})

_RULE = "=" * 72
_SUB = "-" * 72


def _preview_text(text: str | None, max_chars: int | None = None) -> str:
    """Short preview of (preferably already-masked) text for job logs."""
    if not text:
        return ""
    try:
        limit = max_chars
        if limit is None:
            limit = int(get_observability_config().get("max_preview_chars", 120) or 120)
    except Exception:
        limit = 120
    s = str(text).replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def _console(prefix: str, *parts: Any) -> None:
    """Always-on console line for Ray/job tee (mirrors IE diagnostic style)."""
    msg = " | ".join(str(p) for p in parts if p is not None and str(p) != "")
    print(f"[Lane2][{prefix}] {msg}", flush=True)


def _log_step(
    event: str,
    *,
    kind: str,
    block: CanonicalBlock | None = None,
    console: bool = True,
    **fields: Any,
) -> None:
    """Emit structured ``lane2.two_pass.*.{input|logic|output}`` plus optional console line."""
    payload = dict(fields)
    if block is not None:
        payload.setdefault("block_id", block.block_id)
        payload.setdefault("source_file", block.source_file)
        payload.setdefault("block_type", block.block_type)
        payload.setdefault("domain", block.domain)
        payload.setdefault("block_index", block.block_index)
    payload["log_kind"] = kind
    try:
        logger.info(event, **payload)
    except Exception:
        pass
    if console:
        # Compact human line — skip huge nested dumps
        skip = {
            "system_prompt",
            "user_content",
            "raw_response",
            "entities",
            "relationships",
            "allowed_entity_types",
            "allowed_relationship_types",
            "accepted_entities",
            "accepted_relationships",
            "dropped_entities",
            "dropped_relationships",
        }
        bits = [f"{k}={v}" for k, v in payload.items() if k not in skip and k != "log_kind"]
        _console(kind.upper(), event, *bits[:14])


def _maybe_payload_fields(
    *,
    system_prompt: str | None = None,
    user_content: str | None = None,
    raw_response: str | None = None,
) -> dict[str, Any]:
    """Include full LLM payloads only when observability allows it."""
    out: dict[str, Any] = {}
    include = False
    try:
        include = log_llm_payloads_to_server_logs()
    except Exception:
        include = False
    if include:
        if system_prompt is not None:
            out["system_prompt"] = system_prompt
        if user_content is not None:
            out["user_content"] = user_content
        if raw_response is not None:
            out["raw_response"] = raw_response
    else:
        if system_prompt is not None:
            out["system_prompt_ref"] = redact_for_log(system_prompt)
        if user_content is not None:
            out["user_content_ref"] = redact_for_log(user_content)
            out["user_content_preview"] = _preview_text(user_content)
        if raw_response is not None:
            out["raw_response_ref"] = redact_for_log(raw_response)
            out["raw_response_preview"] = _preview_text(raw_response)
    return out


def _entity_summary(entities: list[dict[str, Any]], limit: int = 12) -> list[str]:
    rows: list[str] = []
    for e in entities[:limit]:
        if not isinstance(e, dict):
            continue
        rows.append(f"{e.get('type')}={e.get('value')}@{e.get('confidence')}")
    if len(entities) > limit:
        rows.append(f"...(+{len(entities) - limit} more)")
    return rows


def _entity_policy_rejection_reason(
    entity_type: str, value: str, guidance: dict[str, dict[str, Any]]
) -> str | None:
    """Reject exact generic names explicitly listed by the active type policy."""
    rejected_names = (guidance.get(entity_type) or {}).get("reject_names") or []
    rejected = {str(name).strip().casefold() for name in rejected_names if str(name).strip()}
    if value.strip().casefold() in rejected:
        return f"policy_rejected_generic_name:{entity_type}"
    return None


def _normalize_pass2_entity(
    raw_entity: dict[str, Any],
    *,
    entity_type: str,
    category: str,
    pack: CategoryExtractionPrompt,
    block: CanonicalBlock,
) -> dict[str, Any]:
    """Normalize one Pass 2 entity while preserving optional description/aliases."""
    normalized: dict[str, Any] = {
        "type": entity_type,
        "value": str(raw_entity.get("value") or "")[:80],
        "confidence": float(raw_entity.get("confidence") or 0.0),
        "classified_node_type": category,
        "extraction_prompt_id": pack.prompt_id,
        "source_block_id": block.block_id,
        "source_file": block.source_file,
    }
    raw_desc = raw_entity.get("description")
    description = raw_desc.strip() if isinstance(raw_desc, str) else ""
    if description.casefold() == normalized["value"].strip().casefold():
        description = ""
    if not description:
        description = extract_entity_description_from_text(
            block.text or "", entity_type, normalized["value"]
        ) or ""
    if description:
        normalized["description"] = description[:2000]
    raw_aliases = raw_entity.get("aliases")
    if isinstance(raw_aliases, list):
        aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]
        if aliases:
            normalized["aliases"] = aliases[:10]
    return normalized


def _rel_summary(relationships: list[dict[str, Any]], limit: int = 12) -> list[str]:
    rows: list[str] = []
    for r in relationships[:limit]:
        if not isinstance(r, dict):
            continue
        rows.append(
            f"{r.get('from_type')}:{r.get('from_value')} "
            f"-[{r.get('rel_type')}]-> "
            f"{r.get('to_type')}:{r.get('to_value')}@{r.get('confidence')}"
        )
    if len(relationships) > limit:
        rows.append(f"...(+{len(relationships) - limit} more)")
    return rows


def _review_print(enabled: bool, *lines: str) -> None:
    """Human-readable console output for local demo review (not for production PII logs)."""
    if not enabled:
        return
    for line in lines:
        print(line, flush=True)


def _review_section(enabled: bool, title: str) -> None:
    _review_print(enabled, "", _RULE, title, _RULE)


def _review_inputs(
    enabled: bool,
    *,
    step: str,
    system_prompt: str,
    user_content: str,
) -> None:
    _review_print(
        enabled,
        "",
        _SUB,
        f"INPUT → {step}",
        _SUB,
        f"system_prompt ({len(system_prompt)} chars):",
        system_prompt,
        "",
        f"user_content ({len(user_content)} chars):",
        user_content,
        _SUB,
        f"Calling LLM for {step}...",
        _SUB,
    )


def _review_llm_output(
    enabled: bool,
    *,
    step: str,
    raw_response: str | None,
    duration_ms: float | None,
    summary_lines: list[str],
) -> None:
    raw = raw_response or ""
    _review_print(
        enabled,
        "",
        _SUB,
        f"OUTPUT ← {step}"
        + (f"  ({duration_ms:.1f} ms)" if duration_ms is not None else ""),
        _SUB,
        f"raw_response ({len(raw)} chars):",
        raw if raw else "(empty)",
        "",
        "parsed summary:",
        *[f"  {line}" for line in summary_lines],
        _SUB,
    )

# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class ClassifiedNodeType(BaseModel):
    """One ontology node type / block_category identified in a block."""

    category: str
    confidence: float = 0.0


class ClassificationResult(BaseModel):
    """Pass 1 classification outcome.

    Backward compatible: ``category`` remains the primary (highest-confidence)
    type. ``categories`` holds the full multi-type list (may be length 1).
    """

    category: str | None = None
    confidence: float = 0.0
    categories: list[ClassifiedNodeType] = Field(default_factory=list)
    unmatched: bool = False
    reasoning_summary: str = ""  # short user-reviewable; NO chain-of-thought
    raw_response: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    prompt_id: str = PASS1_PROMPT_ID
    prompt_version: str = PASS1_PROMPT_VERSION
    llm_duration_ms: float | None = None
    token_usage: dict[str, Any] | None = None

    @property
    def identified_categories(self) -> list[str]:
        """Ordered unique category names (primary first)."""
        names: list[str] = []
        seen: set[str] = set()
        for item in self.categories:
            if item.category and item.category not in seen:
                seen.add(item.category)
                names.append(item.category)
        if self.category and self.category not in seen:
            names.insert(0, self.category)
        return names

    @property
    def is_valid(self) -> bool:
        """Valid when at least one ontology category is set and no hard errors."""
        if self.unmatched:
            return False
        if not self.identified_categories:
            return False
        return not any(
            e == "missing_category"
            or e == "malformed_json"
            or e == "unmatched"
            or e.startswith("llm_error")
            for e in self.validation_errors
        )


class ExtractionProvenance(BaseModel):
    """Structured provenance for one type-specific extraction call."""

    source_block_id: str | None = None
    source_text_preview: str | None = None
    source_document: str | None = None
    classified_node_type: str | None = None
    extraction_prompt_id: str | None = None
    extraction_prompt_version: str | None = None
    lane: str = "lane2"


class ExtractionResult(BaseModel):
    """Pass 2 category-specific extraction outcome."""

    classification: ClassificationResult
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str | None = None
    provenance_detail: ExtractionProvenance | None = None
    incoming_node_types: list[str] = Field(default_factory=list)
    outgoing_node_types: list[str] = Field(default_factory=list)
    allowed_relationship_types: list[str] = Field(default_factory=list)
    allowed_relationship_triples: list[tuple[str, str, str]] = Field(default_factory=list)
    classified_node_type: str | None = None
    confidence: float = 0.0
    raw_response: str | None = None
    validation_errors: list[str] = Field(default_factory=list)
    prompt_id: str | None = None
    prompt_version: str = PASS2_PROMPT_VERSION
    llm_duration_ms: float | None = None
    token_usage: dict[str, Any] | None = None
    mocked_ontology_or_prompt_provider: bool = False
    failed: bool = False


class Lane2TwoPassResult(BaseModel):
    """Combined reviewable result for one block (no graph writes)."""

    block_id: str
    source_file: str
    block_index: int | None = None
    block_type: str | None = None
    domain: str | None = None
    heading_context: str | None = None
    character_count: int = 0
    semantic_unit_id: str | None = None
    source_block_ids: list[str] | None = None
    classification: ClassificationResult
    extraction: ExtractionResult | None = None
    extractions: list[ExtractionResult] = Field(default_factory=list)
    extraction_failures: list[dict[str, Any]] = Field(default_factory=list)
    pass2_invoked: bool = False
    total_duration_ms: float = 0.0
    mocked_ontology_or_prompt_provider: bool = False
    success: bool = False
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Prompt provider protocol + implementations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryExtractionPrompt:
    """Category-scoped extraction prompt pack."""

    category: str
    prompt_id: str
    prompt_version: str
    system_prompt: str
    allowed_entity_types: list[str]
    allowed_relationship_types: list[str]
    allowed_relationship_triples: list[tuple[str, str, str]] = field(default_factory=list)
    incoming_node_types: list[str] = field(default_factory=list)
    outgoing_node_types: list[str] = field(default_factory=list)
    is_mocked: bool = False
    entity_type_guidance: dict[str, dict[str, Any]] = field(default_factory=dict)


@runtime_checkable
class CategoryExtractionPromptProvider(Protocol):
    """Replaceable provider for category-specific Pass 2 prompt packs."""

    @property
    def is_mocked(self) -> bool:
        ...

    def get_prompt(self, category: str) -> CategoryExtractionPrompt:
        ...

    def supported_categories(self) -> list[str]:
        ...


def _node_type_by_name(ontology: dict, name: str) -> dict[str, Any] | None:
    for nt in ontology.get("node_types", []):
        if isinstance(nt, dict) and nt.get("name") == name:
            return nt
    return None


def _maps_to_node(ontology: dict, category: str) -> str | None:
    mapping = ontology.get("block_category_to_node") or {}
    if category in mapping:
        return mapping[category]
    for cat in ontology.get("block_categories", []):
        if cat.get("name") == category:
            return cat.get("maps_to_node") or cat.get("default_node_type")
    return None


def _related_types_from_ie_examples(node_def: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Collect entity/rel types appearing in ie_examples for a node type."""
    entity_types: list[str] = []
    rel_types: list[str] = []
    seen_e: set[str] = set()
    seen_r: set[str] = set()
    for ex in node_def.get("ie_examples") or []:
        for ent in ex.get("entities") or []:
            t = ent.get("type")
            if t and t not in seen_e:
                seen_e.add(t)
                entity_types.append(t)
        for rel in ex.get("relationships") or []:
            r = rel.get("rel_type")
            if r and r not in seen_r:
                seen_r.add(r)
                rel_types.append(r)
    return entity_types, rel_types


def _filter_relationship_types(
    ontology: dict,
    allowed_entity_types: list[str],
    preferred_rel_types: list[str] | None = None,
) -> list[str]:
    """Keep relationship types that touch allowed entity types (or preferred list)."""
    allowed = set(allowed_entity_types)
    preferred = set(preferred_rel_types or [])
    names: list[str] = []
    for rt in ontology.get("relationship_types", []):
        name = rt.get("name")
        if not name:
            continue
        if preferred and name not in preferred:
            # Still include if endpoints overlap allowed types
            pass
        from_t = rt.get("from", "Any")
        to_t = rt.get("to", "Any")
        from_list = from_t if isinstance(from_t, list) else [from_t]
        to_list = to_t if isinstance(to_t, list) else [to_t]
        from_ok = "Any" in from_list or bool(allowed.intersection(from_list))
        to_ok = "Any" in to_list or bool(allowed.intersection(to_list))
        if preferred:
            if name in preferred or (from_ok and to_ok):
                names.append(name)
        elif from_ok and to_ok:
            names.append(name)
    return names


def _endpoint_list(value: Any) -> list[str]:
    if value is None:
        return ["Any"]
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def get_ontology_neighborhood(
    ontology: dict[str, Any],
    node_type: str,
) -> dict[str, Any]:
    """Derive permitted node types from exact ontology relationship triples.

    Incoming = types that may point *to* ``node_type``.
    Outgoing = types that ``node_type`` may point *to*.

    Exact relationship triples are authoritative. Endpoint lists are never
    recombined independently.
    """
    incoming: list[str] = []
    outgoing: list[str] = []
    rel_names: list[str] = []
    seen_in: set[str] = set()
    seen_out: set[str] = set()
    seen_rel: set[str] = set()

    relationship_defs = {
        str(rt.get("name")): rt
        for rt in ontology.get("relationship_types", []) or []
        if isinstance(rt, dict) and rt.get("name")
    }

    triples = sorted(compile_relationship_triples(relationship_defs))

    for from_type, rel_name, to_type in triples:
        if node_type not in (from_type, to_type):
            continue

        if rel_name not in seen_rel:
            seen_rel.add(rel_name)
            rel_names.append(rel_name)

        if to_type == node_type and from_type != node_type:
            if from_type not in seen_in:
                seen_in.add(from_type)
                incoming.append(from_type)

        if from_type == node_type and to_type != node_type:
            if to_type not in seen_out:
                seen_out.add(to_type)
                outgoing.append(to_type)

    return {
        "node_type": node_type,
        "incoming_node_types": incoming,
        "outgoing_node_types": outgoing,
        "relationship_types": rel_names,
        "allowed_relationship_triples": triples,
    }


def _build_category_extraction_system_prompt(
    category: str,
    primary_node: str,
    node_def: dict[str, Any] | None,
    allowed_entity_types: list[str],
    allowed_relationship_types: list[str],
    allowed_relationship_triples: list[tuple[str, str, str]] | None = None,
    incoming_node_types: list[str] | None = None,
    outgoing_node_types: list[str] | None = None,
    entity_type_guidance: dict[str, dict[str, Any]] | None = None,
    relationship_guidance: dict[str, dict[str, Any]] | None = None,
) -> str:
    description = (node_def or {}).get("description", "")
    name_pattern = (node_def or {}).get("name_pattern", "")
    anti = (node_def or {}).get("anti_examples") or []
    examples = (node_def or {}).get("ie_examples") or []
    incoming_node_types = list(incoming_node_types or [])
    outgoing_node_types = list(outgoing_node_types or [])
    allowed_relationship_triples = list(allowed_relationship_triples or [])
    entity_type_guidance = dict(entity_type_guidance or {})
    relationship_guidance = dict(relationship_guidance or {})

    lines = [
        f"You are a category-specific information extraction system for block_category={category}.",
        f"Extract ONLY entities and relationships relevant to {category}.",
        "Do NOT reclassify the block. Do NOT invent other block categories.",
        "",
        f"PRIMARY NODE TYPE: {primary_node}",
        "When the source supports an instance of the primary node type, you MUST include that primary node in entities.",
        "Do not replace a supported primary node with only related entities. Do not invent a primary node when the source does not support one.",
        "Extract every relationship explicitly stated or unambiguously described in the source when both endpoints are extracted and the exact triple is allowed.",
        "Do not omit an evidenced allowed relationship, and do not infer a relationship that the source does not support.",
    ]
    if description:
        lines.append(f"Description: {description}")
    if name_pattern:
        lines.append(f"Naming: {name_pattern}")
    lines.append("")
    lines.append("ALLOWED ENTITY TYPES (use EXACT names):")
    for t in allowed_entity_types:
        lines.append(f"  - {t}")
    lines.append("")
    lines.append("PERMITTED INCOMING NODE TYPES (may link toward the primary type):")
    if incoming_node_types:
        for t in incoming_node_types:
            lines.append(f"  - {t}")
    else:
        lines.append("  - (none constrained beyond allowed entity types)")
    lines.append("")
    lines.append("PERMITTED OUTGOING NODE TYPES (primary type may link toward):")
    if outgoing_node_types:
        for t in outgoing_node_types:
            lines.append(f"  - {t}")
    else:
        lines.append("  - (none constrained beyond allowed entity types)")
    lines.append("")
    lines.append("TYPE-SPECIFIC GUIDANCE FOR EVERY ALLOWED ENTITY TYPE (authoritative):")
    for entity_type in allowed_entity_types:
        guidance = entity_type_guidance.get(entity_type) or {}
        lines.append(f"  {entity_type}:")
        if guidance.get("description"):
            lines.append(f"    Meaning: {guidance['description']}")
        if guidance.get("name_pattern"):
            lines.append(f"    Naming rule: {guidance['name_pattern']}")
        for example in (guidance.get("examples") or [])[:5]:
            lines.append(f"    Valid name example: {example}")
        for rejected_name in (guidance.get("reject_names") or [])[:20]:
            lines.append(f"    Reject this generic name exactly: {rejected_name}")
        for anti_example in (guidance.get("anti_examples") or [])[:8]:
            lines.append(f"    Exclusion: {anti_example}")
        for example in (guidance.get("ie_examples") or [])[:2]:
            snippet = str(example.get("block_text") or "").strip().replace("\n", " ")
            example_entities = example.get("entities") or []
            compact_entities = [
                {key: entity.get(key) for key in ("type", "value", "description") if entity.get(key)}
                for entity in example_entities[:8]
                if isinstance(entity, dict)
            ]
            if snippet or compact_entities:
                lines.append(f"    Example source: {snippet[:280]}")
                lines.append(f"    Example entities: {json.dumps(compact_entities, ensure_ascii=False)}")
    lines.append("")
    lines.append("ALLOWED RELATIONSHIP TYPES (use EXACT names):")
    for r in allowed_relationship_types:
        lines.append(f"  - {r}")
    lines.append("")
    lines.append("ALLOWED RELATIONSHIP TRIPLES (EXACT CONTRACT — AUTHORITATIVE):")
    if allowed_relationship_triples:
        for from_type, rel_type, to_type in allowed_relationship_triples:
            lines.append(f"  - {from_type} -[{rel_type}]-> {to_type}")
    else:
        lines.append("  - (none)")
    for relationship_type in allowed_relationship_types:
        guidance = relationship_guidance.get(relationship_type) or {}
        if guidance.get("description"):
            lines.append(f"    {relationship_type} meaning: {guidance['description']}")
        for example in (guidance.get("examples") or [])[:3]:
            lines.append(f"    Evidence example: {example}")
        for example in (guidance.get("anti_examples") or [])[:3]:
            lines.append(f"    Do not infer: {example}")
    if anti:
        lines.append("")
        lines.append("ANTI-EXAMPLES:")
        for a in anti[:8]:
            lines.append(f"  - {a}")
    if examples:
        lines.append("")
        lines.append("FEW-SHOT (extraction shape only; category already known):")
        for i, ex in enumerate(examples[:2], 1):
            sample = {
                "entities": ex.get("entities", []),
                "relationships": ex.get("relationships", []),
                "confidence": 0.9,
                "provenance": None,
            }
            lines.append(f"Example {i} input snippet: {ex.get('block_text', '')[:240]}")
            lines.append(f"Example {i} output:")
            lines.append(json.dumps(sample, indent=2))

    lines.extend(
        [
            "",
            "OUTPUT: Return a SINGLE JSON object (not an array) with keys:",
            '  entities: list of {"type","value","confidence","description"} plus optional "aliases"',
            "Description contract by type (source evidence only; omit description when the source gives no detail):",
            "  Requirement: full obligation; Rule: condition and resulting decision; Control: mechanism and what it checks/enforces.",
            "  Regulation: stated mandate or relevance; UserStory: full role/goal/benefit intent; AcceptanceCriteria: full Given/When/Then criterion.",
            "  Keep identifiers/titles in value and explanatory text in description. Never invent or expand a description from outside the source.",
            '  relationships: list of {"from_type","from_value","rel_type","to_type","to_value","confidence"}',
            "  confidence: float 0..1 for overall extraction quality",
            "  provenance: optional short source citation string, or null",
            "Entity value MUST be a short name or stable ID (1-5 words / ID token, max ~80 chars).",
            "For ID-based artifacts (Requirement/AcceptanceCriteria/TestCase/Defect/Incident/UserStory IDs):",
            "  keep the ID in value; put the source statement/title/summary in optional description.",
            "  Do NOT put 'ID : description' into value. Do NOT invent descriptions for ID-only references.",
            "Every proposed relationship MUST exactly match one of the ALLOWED RELATIONSHIP TRIPLES.",
            "Do NOT combine a relationship name with arbitrary incoming/outgoing endpoint types.",
            "The exact triple contract is authoritative.",
            "Return ONLY JSON.",
        ]
    )
    return "\n".join(lines)


def build_lane2_ontology(
    *,
    customer_ontology: dict[str, Any],
    canonical_model: dict[str, Any],
    extraction_policy: dict[str, Any],
) -> dict[str, Any]:
    """Compose the frozen runtime contracts into Lane2's prompt shape.

    Objective: keep Lane2 category extraction driven by the customer ontology,
    canonical graph model, and industry/domain extraction policy. The invariant
    is that existing category prompts and exact relationship-triple constraints
    remain unchanged as the runtime source moves away from the legacy monolith.
    """
    merged = dict(customer_ontology)

    for key in (
        "block_categories",
        "node_types",
        "relationship_types",
        "vision_ie_type_mapping",
        "path_format",
    ):
        if key in canonical_model:
            merged[key] = canonical_model[key]

    policy_nodes = {
        str(item.get("name")): item
        for item in extraction_policy.get("node_types", []) or []
        if isinstance(item, dict) and item.get("name")
    }

    policy_relationships = {
        str(item.get("name")): item
        for item in extraction_policy.get("relationship_types", []) or []
        if isinstance(item, dict) and item.get("name")
    }

    policy_categories = {
        str(item.get("name")): item
        for item in extraction_policy.get("block_categories", []) or []
        if isinstance(item, dict) and item.get("name")
    }
    merged_categories = []
    for category in merged.get("block_categories", []) or []:
        if not isinstance(category, dict):
            continue
        item = dict(category)
        policy = policy_categories.get(str(item.get("name")))
        if policy:
            # The canonical model owns category identity and node mapping; the
            # industry policy owns definitions, examples, and exclusions.
            for field in ("description", "examples", "anti_examples"):
                if field in policy:
                    item[field] = policy[field]
        merged_categories.append(item)
    merged["block_categories"] = merged_categories

    merged_nodes = []
    for node in merged.get("node_types", []) or []:
        if not isinstance(node, dict):
            continue
        item = dict(node)
        policy = policy_nodes.get(str(item.get("name")))
        if policy:
            item.update(policy)
        merged_nodes.append(item)

    merged_relationships = []
    for relationship in merged.get("relationship_types", []) or []:
        if not isinstance(relationship, dict):
            continue
        item = dict(relationship)
        policy = policy_relationships.get(str(item.get("name")))
        if policy:
            item.update(policy)
        merged_relationships.append(item)

    merged["node_types"] = merged_nodes
    merged["relationship_types"] = merged_relationships

    return merged


def _filter_ontology_examples_for_scope(
    ontology: dict[str, Any], run_scope: dict[str, Any]
) -> dict[str, Any]:
    """Keep industry examples plus only examples matching the frozen run scope."""
    if not run_scope:
        return ontology

    def matches(example: Any) -> bool:
        if not isinstance(example, dict):
            return True
        scope = example.get("scope") or {}
        if not isinstance(scope, dict) or not scope:
            return True
        for key in ("industry_id", "domain_id", "product_line_id", "product_line_name"):
            expected = scope.get(key)
            actual = run_scope.get(key)
            if expected is not None and str(expected).casefold() != str(actual or "").casefold():
                return False
        return True

    filtered = dict(ontology)
    for key in ("node_types", "relationship_types", "block_categories"):
        rows = []
        for row in ontology.get(key, []) or []:
            if not isinstance(row, dict):
                rows.append(row)
                continue
            item = dict(row)
            for examples_key in ("ie_examples", "examples"):
                values = item.get(examples_key)
                if isinstance(values, list):
                    item[examples_key] = [value for value in values if matches(value)]
            rows.append(item)
        filtered[key] = rows
    return filtered


class YamlOntologyProvider:
    """Build Pass 2 prompts from the frozen runtime contracts."""

    def __init__(
        self,
        ontology: dict[str, Any] | None = None,
        *,
        canonical_model: dict[str, Any] | None = None,
        extraction_policy: dict[str, Any] | None = None,
        run_scope: dict[str, Any] | None = None,
    ):
        if ontology is None:
            raise RuntimeError(
                "Lane2 ontology provider requires the authoritative runtime ontology"
            )

        if canonical_model is None or extraction_policy is None:
            # Backward-compatible path for existing tests/callers.
            self._ontology = ontology
        else:
            self._ontology = build_lane2_ontology(
                customer_ontology=ontology,
                canonical_model=canonical_model,
                extraction_policy=extraction_policy,
            )

        self._run_scope = dict(run_scope or {})
        self._ontology = _filter_ontology_examples_for_scope(self._ontology, self._run_scope)
        self._is_mocked = False

    @property
    def is_mocked(self) -> bool:
        return self._is_mocked

    def supported_categories(self) -> list[str]:
        return get_allowed_category_names(self._ontology)

    def get_prompt(self, category: str) -> CategoryExtractionPrompt:
        allowed_cats = set(self.supported_categories())
        if category not in allowed_cats:
            raise ValueError(f"Unknown category for extraction: {category}")

        primary = _maps_to_node(self._ontology, category) or category
        seen: set[str] = set()
        # Semantic-only categories still get a narrow narrative pack
        if primary == "_SKIP_KG":
            primary_for_lookup = category if _node_type_by_name(self._ontology, category) else "Narrative"
            node_def = _node_type_by_name(self._ontology, primary_for_lookup)
            allowed_entity_types = [primary_for_lookup] if node_def else []
            preferred_rels: list[str] = []
            for t in allowed_entity_types:
                seen.add(t)
        else:
            node_def = _node_type_by_name(self._ontology, primary)
            example_ents, preferred_rels = _related_types_from_ie_examples(node_def or {})
            allowed_entity_types = []
            for t in [primary, *example_ents]:
                if t and t not in seen and t in set(get_allowed_node_types(self._ontology)):
                    seen.add(t)
                    allowed_entity_types.append(t)
            if not allowed_entity_types and primary != "_SKIP_KG":
                allowed_entity_types = [primary]
                seen.add(primary)

        allowed_rels = _filter_relationship_types(
            self._ontology, allowed_entity_types, preferred_rels or None
        )
        if not allowed_rels:
            # Fall back to ontology-wide list filtered by name presence
            allowed_rels = [
                r
                for r in get_allowed_relationship_types(self._ontology)
                if r in set(preferred_rels) or not preferred_rels
            ][:12]

        primary_for_neighborhood = (
            primary if primary != "_SKIP_KG" else (allowed_entity_types[0] if allowed_entity_types else category)
        )
        neighborhood = get_ontology_neighborhood(self._ontology, primary_for_neighborhood)
        # Prefer neighborhood rels when non-empty, else keep filtered list
        if neighborhood["relationship_types"]:
            # Intersection preference: keep order from allowed_rels, then append extras
            neigh_set = set(neighborhood["relationship_types"])
            merged_rels = [r for r in allowed_rels if r in neigh_set]
            for r in neighborhood["relationship_types"]:
                if r not in set(merged_rels):
                    merged_rels.append(r)
            if merged_rels:
                allowed_rels = merged_rels
            # Ensure related entity types from neighborhood are extractable
            for t in neighborhood["incoming_node_types"] + neighborhood["outgoing_node_types"]:
                if t in set(get_allowed_node_types(self._ontology)) and t not in seen:
                    seen.add(t)
                    allowed_entity_types.append(t)

        entity_type_guidance = {
            entity_type: dict(_node_type_by_name(self._ontology, entity_type) or {})
            for entity_type in allowed_entity_types
        }
        system_prompt = _build_category_extraction_system_prompt(
            category=category,
            primary_node=primary_for_neighborhood,
            node_def=node_def,
            allowed_entity_types=allowed_entity_types,
            allowed_relationship_types=allowed_rels,
            allowed_relationship_triples=list(
                neighborhood["allowed_relationship_triples"]
            ),
            incoming_node_types=neighborhood["incoming_node_types"],
            outgoing_node_types=neighborhood["outgoing_node_types"],
            entity_type_guidance=entity_type_guidance,
            relationship_guidance={
                str(item.get("name")): item
                for item in self._ontology.get("relationship_types", []) or []
                if isinstance(item, dict) and item.get("name") in allowed_rels
            },
        )
        if self._run_scope:
            domain_id = self._run_scope.get("domain_id")
            product_line_id = self._run_scope.get("product_line_id")
            product_line_name = self._run_scope.get("product_line_name")
            ontology_version = self._run_scope.get("customer_ontology_version")
            scope_lines = [
                "FROZEN INGESTION SCOPE (authoritative; do not infer or change it):",
                (
                    f"Domain: {self._run_scope.get('domain_display_name')} (ID: {domain_id})"
                    if domain_id and self._run_scope.get("domain_display_name")
                    else (f"Domain: {domain_id}" if domain_id else None)
                ),
                f"Selected product line ID: {product_line_id}" if product_line_id else None,
                f"Selected product line: {product_line_name}" if product_line_name else None,
                f"Customer ontology version: {ontology_version}" if ontology_version is not None else None,
                "Extract only information relevant to this selected scope. Do not select a different product line based on document content.",
            ]
            system_prompt += "\n\n" + "\n".join(
                line for line in scope_lines if line
            )

        return CategoryExtractionPrompt(
            category=category,
            prompt_id=f"{PASS2_PROMPT_ID_PREFIX}_{category}",
            prompt_version=PASS2_PROMPT_VERSION,
            system_prompt=system_prompt,
            allowed_entity_types=allowed_entity_types,
            allowed_relationship_types=allowed_rels,
            allowed_relationship_triples=list(
                neighborhood["allowed_relationship_triples"]
            ),
            incoming_node_types=list(neighborhood["incoming_node_types"]),
            outgoing_node_types=list(neighborhood["outgoing_node_types"]),
            is_mocked=False,
            entity_type_guidance=entity_type_guidance,
        )


class MockCategoryExtractionProvider:
    """Clearly marked mock Pass 2 packs — swap for Aurthur category packs when ready.

    Uses ontology categories for validation but ships focused mock prompt text
    for Process/Rule/Requirement/Control. Other categories get a thin mock stub.
    """

    def __init__(self, ontology: dict[str, Any] | None = None):
        if ontology is None:
            raise RuntimeError(
                "Lane2 ontology provider requires the authoritative runtime ontology"
            )
        self._ontology = ontology
        self._yaml = YamlOntologyProvider(self._ontology)
        self._packs = self._build_mock_packs()

    @property
    def is_mocked(self) -> bool:
        return True

    def supported_categories(self) -> list[str]:
        return get_allowed_category_names(self._ontology)

    def _build_mock_packs(self) -> dict[str, CategoryExtractionPrompt]:
        packs: dict[str, CategoryExtractionPrompt] = {}
        # Seed from YAML structure, then override copy for mock clarity
        for cat in self.supported_categories():
            base = self._yaml.get_prompt(cat)
            if cat in _MOCK_PACK_CATEGORIES:
                system = (
                    f"[MOCK CategoryExtractionPromptProvider — replace with Aurthur pack]\n"
                    f"Category-specific extraction for {cat} ONLY.\n"
                    f"Allowed entity types: {', '.join(base.allowed_entity_types)}\n"
                    f"Allowed relationship types: {', '.join(base.allowed_relationship_types)}\n"
                    "Return a single JSON object with entities, relationships, confidence, provenance.\n"
                    "Do not reclassify. Do not extract unrelated categories.\n"
                )
                packs[cat] = CategoryExtractionPrompt(
                    category=cat,
                    prompt_id=f"{PASS2_PROMPT_ID_PREFIX}_{cat}_mock",
                    prompt_version=PASS2_PROMPT_VERSION,
                    system_prompt=system + "\n" + base.system_prompt,
                    allowed_entity_types=list(base.allowed_entity_types),
                    allowed_relationship_types=list(base.allowed_relationship_types),
                    incoming_node_types=list(base.incoming_node_types),
                    outgoing_node_types=list(base.outgoing_node_types),
                    is_mocked=True,
                    entity_type_guidance=dict(base.entity_type_guidance),
                )
            else:
                packs[cat] = CategoryExtractionPrompt(
                    category=cat,
                    prompt_id=f"{PASS2_PROMPT_ID_PREFIX}_{cat}_mock",
                    prompt_version=PASS2_PROMPT_VERSION,
                    system_prompt=(
                        f"[MOCK CategoryExtractionPromptProvider — replace with Aurthur pack]\n"
                        + base.system_prompt
                    ),
                    allowed_entity_types=list(base.allowed_entity_types),
                    allowed_relationship_types=list(base.allowed_relationship_types),
                    incoming_node_types=list(base.incoming_node_types),
                    outgoing_node_types=list(base.outgoing_node_types),
                    is_mocked=True,
                    entity_type_guidance=dict(base.entity_type_guidance),
                )
        return packs

    def get_prompt(self, category: str) -> CategoryExtractionPrompt:
        if category not in self._packs:
            raise ValueError(f"No mock extraction pack for category: {category}")
        return self._packs[category]


# ---------------------------------------------------------------------------
# LLM callable protocol (injectable; mock in tests)
# ---------------------------------------------------------------------------


LLMCallable = Callable[[str, str], tuple[str, dict[str, Any]]]
# (system_prompt, user_content) -> (response_text, metadata)


def _parse_json_object(response_text: str) -> dict[str, Any] | None:
    """Best-effort parse of a single JSON object from LLM text."""
    if not response_text or not str(response_text).strip():
        return None
    text = str(response_text).strip()

    # Markdown fence
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Direct object
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
    except json.JSONDecodeError:
        pass

    # First {...} span
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def build_pass1_system_prompt(ontology: dict[str, Any] | None = None) -> str:
    """Multi-type classification prompt: ontology categories + decision tree."""
    if ontology is None:
        raise RuntimeError(
            "Lane2 requires the authoritative runtime ontology"
        )
    categories_section = build_block_categories_section(ontology, multi=True)
    allowed = get_allowed_category_names(ontology)
    return "\n".join(
        [
            "You are an information-classification engine for enterprise knowledge bases.",
            "Compare the input block against the supplied ontology definitions and examples.",
            "Identify EVERY ontology node type / block_category that is sufficiently supported",
            "by evidence in the block. A single block may contain multiple types",
            "(for example Requirement, Process, Control, Rule, and Defect).",
            "Do NOT invent unsupported node types. Do NOT extract entities or relationships.",
            "Do NOT use Actor or System as block categories (they are node types).",
            "If no ontology type is sufficiently supported, return an empty categories list",
            'and set unmatched=true.',
            "",
            categories_section,
            "",
            CLASSIFICATION_DECISION_TREE,
            "",
            "OUTPUT: Return a SINGLE JSON object with keys:",
            '  categories: list of {"category": one of '
            + json.dumps(allowed)
            + ', "confidence": float 0..1}',
            "  unmatched: boolean (true only when categories is empty)",
            "  confidence: float 0..1 overall (max of per-category confidences, or 0 if unmatched)",
            "  reasoning_summary: one short sentence (no chain-of-thought)",
            "For backward compatibility you MAY also include a singular 'category' field equal",
            "to the highest-confidence entry in categories (or null when unmatched).",
            "Return ONLY JSON.",
        ]
    )


def build_pass1_user_content(block: CanonicalBlock, text: str) -> str:
    heading = None
    if isinstance(block.metadata, dict):
        heading = block.metadata.get("heading_context_raw") or block.metadata.get("heading_context")
    parts = [
        f"block_id: {block.block_id}",
        f"source_file: {block.source_file}",
        f"domain: {block.domain}",
        f"block_type: {block.block_type}",
    ]
    if heading:
        parts.append(f"heading_context: {heading}")
    parts.append("")
    parts.append("TEXT:")
    parts.append(text)
    return "\n".join(parts)

def _heading_context(block: CanonicalBlock) -> str | None:
    if not isinstance(block.metadata, dict):
        return None
    value = block.metadata.get("heading_context_raw") or block.metadata.get("heading_context")
    if value is None:
        return None
    return str(value)[:200]


def _block_text_for_llm(block: CanonicalBlock, masked_text: str | None = None) -> str:
    if masked_text is not None:
        return masked_text
    return block.text or ""


def classify_block(
    block: CanonicalBlock,
    *,
    llm_callable: LLMCallable,
    ontology: dict[str, Any] | None = None,
    masked_text: str | None = None,
    review_io: bool = False,
) -> ClassificationResult:
    """Pass 1: classify block into one ontology block_category."""
    if ontology is None:
        raise RuntimeError(
            "Lane2 requires the authoritative runtime ontology"
        )
    allowed = get_allowed_category_names(ontology)
    allowed_set = set(allowed)
    text = _block_text_for_llm(block, masked_text)
    system_prompt = build_pass1_system_prompt(ontology)
    user_content = build_pass1_user_content(block, text)
    heading = _heading_context(block)

    _log_step(
        "lane2.two_pass.pass1.input",
        kind="input",
        block=block,
        pass1_prompt_id=PASS1_PROMPT_ID,
        pass1_prompt_version=PASS1_PROMPT_VERSION,
        heading_context=heading,
        character_count=len(text),
        text_preview=_preview_text(text),
        system_prompt_chars=len(system_prompt),
        user_content_chars=len(user_content),
        allowed_category_count=len(allowed),
        allowed_categories_sample=allowed[:20],
        masked_text_provided=masked_text is not None,
        **_maybe_payload_fields(system_prompt=system_prompt, user_content=user_content),
    )

    logger.info(
        "lane2.two_pass.pass1.start",
        block_id=block.block_id,
        source_file=block.source_file,
        pass1_prompt_id=PASS1_PROMPT_ID,
        pass1_prompt_version=PASS1_PROMPT_VERSION,
        character_count=len(text),
        system_prompt_chars=len(system_prompt),
        user_content_chars=len(user_content),
        allowed_category_count=len(allowed),
    )

    _review_section(review_io, f"PASS 1 — CLASSIFY  |  block_id={block.block_id}")
    _review_inputs(
        review_io,
        step="Pass 1 classify",
        system_prompt=system_prompt,
        user_content=user_content,
    )

    raw_response: str | None = None
    metadata: dict[str, Any] = {}
    try:
        raw_response, metadata = llm_callable(system_prompt, user_content)
    except Exception as exc:  # noqa: BLE001 — prototype must not crash caller
        result = ClassificationResult(
            category=None,
            confidence=0.0,
            reasoning_summary="LLM call failed",
            raw_response=None,
            validation_errors=[f"llm_error: {exc}"],
            llm_duration_ms=None,
        )
        _review_llm_output(
            review_io,
            step="Pass 1 classify",
            raw_response=None,
            duration_ms=None,
            summary_lines=[f"ERROR: {exc}", "is_valid=False"],
        )
        _log_step(
            "lane2.two_pass.pass1.output",
            kind="output",
            block=block,
            success=False,
            is_valid=False,
            pass1_category=None,
            pass1_confidence=0.0,
            pass1_validation_errors=result.validation_errors,
            error=str(exc),
        )
        logger.info(
            "lane2.two_pass.pass1.end",
            block_id=block.block_id,
            source_file=block.source_file,
            pass1_category=None,
            pass1_confidence=0.0,
            pass1_validation_errors=result.validation_errors,
            is_valid=False,
        )
        return result

    duration_ms = metadata.get("duration_ms")
    token_usage = metadata.get("token_usage")
    parsed = _parse_json_object(raw_response or "")
    if parsed is None:
        result = ClassificationResult(
            category=None,
            confidence=0.0,
            reasoning_summary="Malformed LLM response",
            raw_response=raw_response,
            validation_errors=["malformed_json"],
            llm_duration_ms=duration_ms,
            token_usage=token_usage,
        )
        _review_llm_output(
            review_io,
            step="Pass 1 classify",
            raw_response=raw_response,
            duration_ms=duration_ms,
            summary_lines=["malformed_json", "is_valid=False"],
        )
        _log_step(
            "lane2.two_pass.pass1.logic",
            kind="logic",
            block=block,
            step="parse_json",
            parse_ok=False,
            decision="reject_malformed_json",
            **_maybe_payload_fields(raw_response=raw_response),
        )
        _log_step(
            "lane2.two_pass.pass1.output",
            kind="output",
            block=block,
            success=False,
            is_valid=False,
            pass1_category=None,
            pass1_confidence=0.0,
            pass1_validation_errors=result.validation_errors,
            llm_duration_ms=duration_ms,
            **_maybe_payload_fields(raw_response=raw_response),
        )
        logger.info(
            "lane2.two_pass.pass1.end",
            block_id=block.block_id,
            source_file=block.source_file,
            pass1_category=None,
            pass1_confidence=0.0,
            pass1_validation_errors=result.validation_errors,
            llm_duration_ms=duration_ms,
            is_valid=False,
        )
        return result

    raw_category = parsed.get("category") or parsed.get("block_category")
    confidence = float(parsed.get("confidence") or 0.0)
    reasoning = str(parsed.get("reasoning_summary") or parsed.get("reasoning") or "")[:240]
    confidence_raw = confidence

    errors: list[str] = []
    category: str | None = None
    normalized: str | None = None
    if not raw_category or not isinstance(raw_category, str):
        errors.append("missing_category")
        _log_step(
            "lane2.two_pass.pass1.logic",
            kind="logic",
            block=block,
            step="validate_category",
            raw_category=raw_category,
            decision="missing_category",
        )
    else:
        normalized = normalize_to_ontology(raw_category.strip(), allowed)
        if normalized not in allowed_set:
            errors.append(f"invalid_category:{raw_category}")
            _log_step(
                "lane2.two_pass.pass1.logic",
                kind="logic",
                block=block,
                step="normalize_category",
                raw_category=raw_category,
                normalized=normalized,
                in_allowed=False,
                decision="reject_invalid_category",
            )
        else:
            # Prefer enum membership when available
            try:
                category = BlockCategory(normalized).value
            except ValueError:
                errors.append(f"invalid_category:{raw_category}")
                category = None
                _log_step(
                    "lane2.two_pass.pass1.logic",
                    kind="logic",
                    block=block,
                    step="enum_membership",
                    raw_category=raw_category,
                    normalized=normalized,
                    decision="reject_not_in_BlockCategory_enum",
                )
            else:
                category = normalized
                _log_step(
                    "lane2.two_pass.pass1.logic",
                    kind="logic",
                    block=block,
                    step="normalize_category",
                    raw_category=raw_category,
                    normalized=normalized,
                    final_category=category,
                    in_allowed=True,
                    decision="accept_category",
                )

    if confidence < 0.0 or confidence > 1.0:
        errors.append("confidence_out_of_range")
        confidence = max(0.0, min(1.0, confidence))
        _log_step(
            "lane2.two_pass.pass1.logic",
            kind="logic",
            block=block,
            step="clamp_confidence",
            confidence_raw=confidence_raw,
            confidence_clamped=confidence,
            decision="clamp_to_0_1",
        )

    category_invalid = any(
        e == "missing_category" or e.startswith("invalid_category:") for e in errors
    )
    result = ClassificationResult(
        category=None if category_invalid else category,
        confidence=confidence,
        reasoning_summary=reasoning,
        raw_response=raw_response,
        validation_errors=errors,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
    )
    _review_llm_output(
        review_io,
        step="Pass 1 classify",
        raw_response=raw_response,
        duration_ms=duration_ms,
        summary_lines=[
            f"category={result.category}",
            f"confidence={result.confidence}",
            f"reasoning_summary={result.reasoning_summary}",
            f"validation_errors={result.validation_errors}",
            f"is_valid={result.is_valid}",
        ],
    )
    _log_step(
        "lane2.two_pass.pass1.output",
        kind="output",
        block=block,
        success=result.is_valid,
        is_valid=result.is_valid,
        pass1_category=result.category,
        pass1_confidence=result.confidence,
        pass1_reasoning_summary=result.reasoning_summary,
        pass1_validation_errors=result.validation_errors,
        raw_category=raw_category,
        normalized_category=normalized,
        confidence_raw=confidence_raw,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
        **_maybe_payload_fields(raw_response=raw_response),
    )
    logger.info(
        "lane2.two_pass.pass1.end",
        block_id=block.block_id,
        source_file=block.source_file,
        pass1_category=result.category,
        pass1_confidence=result.confidence,
        pass1_reasoning_summary=result.reasoning_summary,
        pass1_validation_errors=result.validation_errors,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
        is_valid=result.is_valid,
    )
    return result


def extract_for_category(
    block: CanonicalBlock,
    category: str,
    *,
    llm_callable: LLMCallable,
    prompt_provider: CategoryExtractionPromptProvider,
    ontology: dict[str, Any] | None = None,
    masked_text: str | None = None,
    classification: ClassificationResult | None = None,
    review_io: bool = False,
) -> ExtractionResult:
    """Pass 2: category-specific entity/relationship extraction."""
    if ontology is None:
        raise RuntimeError(
            "Lane2 requires the authoritative runtime ontology"
        )
    allowed = set(get_allowed_category_names(ontology))
    text = _block_text_for_llm(block, masked_text)
    mocked = bool(getattr(prompt_provider, "is_mocked", False))
    heading = _heading_context(block)

    logger.info(
        "lane2.two_pass.pass2.start",
        block_id=block.block_id,
        source_file=block.source_file,
        selected_pass2_category=category,
        character_count=len(text),
        mocked_ontology_or_prompt_provider=mocked,
    )

    if category not in allowed:
        cls = classification or ClassificationResult(
            category=None,
            validation_errors=[f"invalid_category:{category}"],
        )
        _log_step(
            "lane2.two_pass.pass2.logic",
            kind="logic",
            block=block,
            step="category_gate",
            selected_pass2_category=category,
            decision="skip_pass2_invalid_category",
        )
        logger.info(
            "lane2.two_pass.pass2.contract",
            block_id=block.block_id,
            allowed_relationship_triple_count=0,
            accepted_relationship_count=0,
            dropped_relationship_count=0,
        )

        _log_step(
            "lane2.two_pass.pass2.output",
            kind="output",
            block=block,
            success=False,
            selected_pass2_category=category,
            entity_count=0,
            relationship_count=0,
            pass2_validation_errors=[f"invalid_category_skip_pass2:{category}"],
        )
        logger.info(
            "lane2.two_pass.pass2.end",
            block_id=block.block_id,
            source_file=block.source_file,
            selected_pass2_category=category,
            pass2_invoked=False,
            pass2_validation_errors=[f"invalid_category_skip_pass2:{category}"],
            entity_count=0,
            relationship_count=0,
            mocked_ontology_or_prompt_provider=mocked,
        )
        return ExtractionResult(
            classification=cls,
            validation_errors=[f"invalid_category_skip_pass2:{category}"],
            classified_node_type=category,
            mocked_ontology_or_prompt_provider=mocked,
            failed=True,
        )

    try:
        pack = prompt_provider.get_prompt(category)
    except Exception as exc:  # noqa: BLE001
        cls = classification or ClassificationResult(category=category)
        _log_step(
            "lane2.two_pass.pass2.logic",
            kind="logic",
            block=block,
            step="prompt_provider",
            selected_pass2_category=category,
            decision="prompt_provider_error",
            error=str(exc),
        )
        logger.info(
            "lane2.two_pass.pass2.end",
            block_id=block.block_id,
            source_file=block.source_file,
            selected_pass2_category=category,
            pass2_validation_errors=[f"prompt_provider_error: {exc}"],
            entity_count=0,
            relationship_count=0,
            mocked_ontology_or_prompt_provider=mocked,
        )
        return ExtractionResult(
            classification=cls,
            validation_errors=[f"prompt_provider_error: {exc}"],
            classified_node_type=category,
            mocked_ontology_or_prompt_provider=mocked,
            failed=True,
        )

    logger.info(
        "lane2.two_pass.pass2.prompt_selected",
        block_id=block.block_id,
        source_file=block.source_file,
        selected_pass2_category=category,
        selected_pass2_prompt=pack.prompt_id,
        pass2_prompt_version=pack.prompt_version,
        allowed_entity_type_count=len(pack.allowed_entity_types),
        allowed_relationship_type_count=len(pack.allowed_relationship_types),
        system_prompt_chars=len(pack.system_prompt),
        pack_is_mocked=pack.is_mocked,
        mocked_ontology_or_prompt_provider=mocked or pack.is_mocked,
    )

    user_content = build_pass1_user_content(block, text)
    user_content = (
        f"Known block_category: {category}\n"
        "Extract entities and relationships for this category only.\n\n"
        + user_content
    )

    _log_step(
        "lane2.two_pass.pass2.input",
        kind="input",
        block=block,
        selected_pass2_category=category,
        selected_pass2_prompt=pack.prompt_id,
        pass2_prompt_version=pack.prompt_version,
        heading_context=heading,
        character_count=len(text),
        text_preview=_preview_text(text),
        system_prompt_chars=len(pack.system_prompt),
        user_content_chars=len(user_content),
        allowed_entity_type_count=len(pack.allowed_entity_types),
        allowed_relationship_type_count=len(pack.allowed_relationship_types),
        allowed_entity_types=list(pack.allowed_entity_types)[:30],
        allowed_relationship_types=list(pack.allowed_relationship_types)[:30],
        pack_is_mocked=pack.is_mocked,
        pass1_category=classification.category if classification else category,
        pass1_confidence=classification.confidence if classification else None,
        **_maybe_payload_fields(system_prompt=pack.system_prompt, user_content=user_content),
    )

    _review_section(
        review_io,
        f"PASS 2 — EXTRACT  |  category={category}  |  prompt={pack.prompt_id}",
    )
    _review_print(
        review_io,
        f"allowed_entity_types: {pack.allowed_entity_types}",
        f"allowed_relationship_types: {pack.allowed_relationship_types}",
    )
    _review_inputs(
        review_io,
        step=f"Pass 2 extract ({category})",
        system_prompt=pack.system_prompt,
        user_content=user_content,
    )

    raw_response: str | None = None
    metadata: dict[str, Any] = {}
    try:
        raw_response, metadata = llm_callable(pack.system_prompt, user_content)
    except Exception as exc:  # noqa: BLE001
        cls = classification or ClassificationResult(category=category)
        _review_llm_output(
            review_io,
            step=f"Pass 2 extract ({category})",
            raw_response=None,
            duration_ms=None,
            summary_lines=[f"ERROR: {exc}"],
        )
        _log_step(
            "lane2.two_pass.pass2.output",
            kind="output",
            block=block,
            success=False,
            selected_pass2_category=category,
            selected_pass2_prompt=pack.prompt_id,
            entity_count=0,
            relationship_count=0,
            pass2_validation_errors=[f"llm_error: {exc}"],
            error=str(exc),
        )
        logger.info(
            "lane2.two_pass.pass2.end",
            block_id=block.block_id,
            source_file=block.source_file,
            selected_pass2_category=category,
            selected_pass2_prompt=pack.prompt_id,
            pass2_validation_errors=[f"llm_error: {exc}"],
            entity_count=0,
            relationship_count=0,
            mocked_ontology_or_prompt_provider=bool(prompt_provider.is_mocked or pack.is_mocked),
        )
        return ExtractionResult(
            classification=cls,
            raw_response=None,
            validation_errors=[f"llm_error: {exc}"],
            prompt_id=pack.prompt_id,
            prompt_version=pack.prompt_version,
            classified_node_type=category,
            mocked_ontology_or_prompt_provider=bool(prompt_provider.is_mocked or pack.is_mocked),
            failed=True,
        )

    duration_ms = metadata.get("duration_ms")
    token_usage = metadata.get("token_usage")
    parsed = _parse_json_object(raw_response or "")
    errors: list[str] = []
    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    provenance: str | None = None
    confidence = 0.0
    dropped_entities: list[str] = []
    dropped_relationships: list[str] = []
    raw_entity_count = 0
    raw_rel_count = 0

    if parsed is None:
        errors.append("malformed_json")
        _log_step(
            "lane2.two_pass.pass2.logic",
            kind="logic",
            block=block,
            step="parse_json",
            parse_ok=False,
            decision="reject_malformed_json",
            **_maybe_payload_fields(raw_response=raw_response),
        )
    else:
        ents = parsed.get("entities")
        rels = parsed.get("relationships")
        if ents is None:
            errors.append("missing_entities")
            ents = []
        if rels is None:
            errors.append("missing_relationships")
            rels = []
        if not isinstance(ents, list) or not isinstance(rels, list):
            errors.append("entities_or_relationships_not_list")
            ents, rels = [], []

        raw_entity_count = len(ents) if isinstance(ents, list) else 0
        raw_rel_count = len(rels) if isinstance(rels, list) else 0

        allowed_ent = set(pack.allowed_entity_types) or set(get_allowed_node_types(ontology))
        allowed_rel = set(pack.allowed_relationship_types) or set(
            get_allowed_relationship_types(ontology)
        )
        allowed_triples = {
            tuple(triple)
            for triple in (pack.allowed_relationship_triples or [])
            if isinstance(triple, (list, tuple)) and len(triple) == 3
        }

        _log_step(
            "lane2.two_pass.pass2.logic",
            kind="logic",
            block=block,
            step="parse_json",
            parse_ok=True,
            raw_entity_count=raw_entity_count,
            raw_relationship_count=raw_rel_count,
            allowed_entity_type_count=len(allowed_ent),
            allowed_relationship_type_count=len(allowed_rel),
            decision="filter_against_allowed_types",
        )

        for e in ents:
            if not isinstance(e, dict):
                errors.append("entity_not_object")
                dropped_entities.append("<non-object>")
                continue
            raw_type = str(e.get("type") or "")
            et = normalize_to_ontology(raw_type, list(allowed_ent))
            if et not in allowed_ent:
                errors.append(f"invalid_entity_type:{e.get('type')}")
                dropped_entities.append(f"{raw_type}->{et or '?'}={e.get('value')}")
                continue
            if raw_type and et != raw_type:
                _log_step(
                    "lane2.two_pass.pass2.logic",
                    kind="logic",
                    block=block,
                    console=False,
                    step="normalize_entity_type",
                    raw_type=raw_type,
                    normalized_type=et,
                    value=str(e.get("value") or "")[:80],
                    decision="normalize_accept",
                )
            entity_value = str(e.get("value") or "")[:80]
            rejection_reason = _entity_policy_rejection_reason(
                et, entity_value, pack.entity_type_guidance
            )
            if rejection_reason:
                errors.append(rejection_reason)
                dropped_entities.append(f"{et}={entity_value} ({rejection_reason})")
                _log_step(
                    "lane2.two_pass.pass2.logic",
                    kind="logic",
                    block=block,
                    console=False,
                    step="validate_entity_name_policy",
                    entity_type=et,
                    value=entity_value,
                    decision="reject_policy_generic_name",
                    reason=rejection_reason,
                )
                continue
            entities.append(
                _normalize_pass2_entity(
                    e,
                    entity_type=et,
                    category=category,
                    pack=pack,
                    block=block,
                )
            )

        for r in rels:
            if not isinstance(r, dict):
                errors.append("relationship_not_object")
                dropped_relationships.append("<non-object>")
                continue
            raw_rt = str(r.get("rel_type") or "")
            rt = normalize_to_ontology(raw_rt, list(allowed_rel))
            if rt not in allowed_rel:
                errors.append(f"invalid_rel_type:{r.get('rel_type')}")
                dropped_relationships.append(
                    f"{r.get('from_value')}-[{raw_rt}]->{r.get('to_value')}"
                )
                continue

            from_type = normalize_to_ontology(
                str(r.get("from_type") or ""),
                list(allowed_ent),
            )
            to_type = normalize_to_ontology(
                str(r.get("to_type") or ""),
                list(allowed_ent),
            )

            relationship_triple = (from_type, rt, to_type)

            if relationship_triple not in allowed_triples:
                errors.append(
                    "invalid_relationship_triple:"
                    f"{from_type}-[{rt}]->{to_type}"
                )
                dropped_relationships.append(
                    f"{r.get('from_value')}-[{rt}]->{r.get('to_value')}"
                )
                _log_step(
                    "lane2.two_pass.pass2.logic",
                    kind="logic",
                    block=block,
                    console=False,
                    step="validate_relationship_triple",
                    from_type=from_type,
                    rel_type=rt,
                    to_type=to_type,
                    decision="reject_invalid_relationship_triple",
                )
                continue

            if raw_rt and rt != raw_rt:
                _log_step(
                    "lane2.two_pass.pass2.logic",
                    kind="logic",
                    block=block,
                    console=False,
                    step="normalize_rel_type",
                    raw_rel_type=raw_rt,
                    normalized_rel_type=rt,
                    decision="normalize_accept",
                )

            relationships.append(
                {
                    "from_type": from_type,
                    "from_value": str(r.get("from_value") or "")[:80],
                    "rel_type": rt,
                    "to_type": to_type,
                    "to_value": str(r.get("to_value") or "")[:80],
                    "confidence": float(r.get("confidence") or 0.0),
                    "classified_node_type": category,
                    "extraction_prompt_id": pack.prompt_id,
                    "source_block_id": block.block_id,
                    "source_file": block.source_file,
                }
            )

        provenance = parsed.get("provenance")
        if provenance is not None:
            provenance = str(provenance)[:240]
        try:
            confidence = float(parsed.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
            errors.append("bad_confidence")

        _log_step(
            "lane2.two_pass.pass2.logic",
            kind="logic",
            block=block,
            step="filter_summary",
            raw_entity_count=raw_entity_count,
            accepted_entity_count=len(entities),
            dropped_entity_count=len(dropped_entities),
            raw_relationship_count=raw_rel_count,
            accepted_relationship_count=len(relationships),
            dropped_relationship_count=len(dropped_relationships),
            dropped_entities=dropped_entities[:20],
            dropped_relationships=dropped_relationships[:20],
            decision="keep_ontology_valid_only",
        )

    cls = classification or ClassificationResult(category=category, confidence=confidence)
    entity_types = sorted({e.get("type") for e in entities if e.get("type")})
    provenance_detail = ExtractionProvenance(
        source_block_id=block.block_id,
        source_text_preview=_preview_text(text),
        source_document=block.source_file,
        classified_node_type=category,
        extraction_prompt_id=pack.prompt_id,
        extraction_prompt_version=pack.prompt_version,
        lane="lane2",
    )
    result = ExtractionResult(
        classification=cls,
        entities=entities,
        relationships=relationships,
        provenance=provenance,
        provenance_detail=provenance_detail,
        incoming_node_types=list(pack.incoming_node_types or []),
        outgoing_node_types=list(pack.outgoing_node_types or []),
        allowed_relationship_types=list(pack.allowed_relationship_types or []),
        allowed_relationship_triples=list(pack.allowed_relationship_triples or []),
        classified_node_type=category,
        confidence=confidence,
        raw_response=raw_response,
        validation_errors=errors,
        prompt_id=pack.prompt_id,
        prompt_version=pack.prompt_version,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
        mocked_ontology_or_prompt_provider=bool(prompt_provider.is_mocked or pack.is_mocked),
        failed=any(
            e.startswith("llm_error") or e == "malformed_json" or e.startswith("prompt_provider")
            for e in errors
        ),
    )
    _review_llm_output(
        review_io,
        step=f"Pass 2 extract ({category})",
        raw_response=raw_response,
        duration_ms=duration_ms,
        summary_lines=[
            f"entity_count={len(entities)}  types={entity_types}",
            f"relationship_count={len(relationships)}",
            f"confidence={confidence}",
            f"validation_errors={errors}",
            f"entities={json.dumps(entities, indent=2)}",
            f"relationships={json.dumps(relationships, indent=2)}",
        ],
    )
    _log_step(
        "lane2.two_pass.pass2.output",
        kind="output",
        block=block,
        success=not any(
            e.startswith("llm_error") or e == "malformed_json" or e.startswith("prompt_provider")
            for e in errors
        ),
        selected_pass2_category=category,
        selected_pass2_prompt=pack.prompt_id,
        entity_count=len(entities),
        relationship_count=len(relationships),
        extracted_entity_types=entity_types,
        entity_summary=_entity_summary(entities),
        relationship_summary=_rel_summary(relationships),
        accepted_entities=entities,
        accepted_relationships=relationships,
        pass2_confidence=confidence,
        provenance=provenance,
        pass2_validation_errors=errors,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
        **_maybe_payload_fields(raw_response=raw_response),
    )
    logger.info(
        "lane2.two_pass.pass2.end",
        block_id=block.block_id,
        source_file=block.source_file,
        selected_pass2_category=category,
        selected_pass2_prompt=pack.prompt_id,
        entity_count=len(entities),
        relationship_count=len(relationships),
        extracted_entity_types=entity_types,
        pass2_confidence=confidence,
        pass2_validation_errors=errors,
        llm_duration_ms=duration_ms,
        token_usage=token_usage,
        mocked_ontology_or_prompt_provider=result.mocked_ontology_or_prompt_provider,
    )
    return result


def _entity_key(entity: dict[str, Any]) -> tuple[str, str]:
    return (
        str(entity.get("type") or "").strip().lower(),
        str(entity.get("value") or "").strip().lower(),
    )


def _rel_key(rel: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(rel.get("from_type") or "").strip().lower(),
        str(rel.get("from_value") or "").strip().lower(),
        str(rel.get("rel_type") or "").strip().lower(),
        str(rel.get("to_type") or "").strip().lower(),
        str(rel.get("to_value") or "").strip().lower(),
    )


def merge_extraction_results(
    classification: ClassificationResult,
    extractions: list[ExtractionResult],
) -> ExtractionResult:
    """Merge per-type extractions into one block-level result without overwriting."""
    merged_entities: list[dict[str, Any]] = []
    merged_rels: list[dict[str, Any]] = []
    seen_e: set[tuple[str, str]] = set()
    seen_r: set[tuple[str, str, str, str, str]] = set()
    errors: list[str] = []
    confidences: list[float] = []
    mocked = False
    any_failed = False
    prompt_ids: list[str] = []

    for ext in extractions:
        mocked = mocked or ext.mocked_ontology_or_prompt_provider
        any_failed = any_failed or ext.failed
        if ext.prompt_id:
            prompt_ids.append(ext.prompt_id)
        confidences.append(float(ext.confidence or 0.0))
        for err in ext.validation_errors or []:
            tagged = f"{ext.classified_node_type or ext.prompt_id}:{err}"
            errors.append(tagged)
        for ent in ext.entities or []:
            if not isinstance(ent, dict):
                continue
            key = _entity_key(ent)
            if not key[1] or key in seen_e:
                continue
            seen_e.add(key)
            merged_entities.append(dict(ent))
        for rel in ext.relationships or []:
            if not isinstance(rel, dict):
                continue
            key = _rel_key(rel)
            if not key[2] or key in seen_r:
                continue
            seen_r.add(key)
            merged_rels.append(dict(rel))

    primary_type = classification.category
    primary_ext = next(
        (e for e in extractions if e.classified_node_type == primary_type),
        extractions[0] if extractions else None,
    )
    return ExtractionResult(
        classification=classification,
        entities=merged_entities,
        relationships=merged_rels,
        provenance=(
            f"merged:{','.join(prompt_ids)}" if prompt_ids else None
        ),
        provenance_detail=ExtractionProvenance(
            source_block_id=primary_ext.provenance_detail.source_block_id
            if primary_ext and primary_ext.provenance_detail
            else None,
            source_text_preview=primary_ext.provenance_detail.source_text_preview
            if primary_ext and primary_ext.provenance_detail
            else None,
            source_document=primary_ext.provenance_detail.source_document
            if primary_ext and primary_ext.provenance_detail
            else None,
            classified_node_type=primary_type,
            extraction_prompt_id="merged:" + ",".join(prompt_ids) if prompt_ids else None,
            extraction_prompt_version=PASS2_PROMPT_VERSION,
            lane="lane2",
        ),
        incoming_node_types=list(primary_ext.incoming_node_types) if primary_ext else [],
        outgoing_node_types=list(primary_ext.outgoing_node_types) if primary_ext else [],
        allowed_relationship_types=list(primary_ext.allowed_relationship_types)
        if primary_ext
        else [],
        classified_node_type=primary_type,
        confidence=max(confidences) if confidences else 0.0,
        validation_errors=errors,
        prompt_id="merged:" + ",".join(prompt_ids) if prompt_ids else None,
        prompt_version=PASS2_PROMPT_VERSION,
        mocked_ontology_or_prompt_provider=mocked,
        failed=any_failed and not merged_entities and not merged_rels,
    )


def process_lane2_block(
    block: CanonicalBlock,
    *,
    llm_callable: LLMCallable,
    prompt_provider: CategoryExtractionPromptProvider | None = None,
    ontology: dict[str, Any] | None = None,
    masked_text: str | None = None,
    review_io: bool = False,
) -> Lane2TwoPassResult:
    """Run Pass 1 then Pass 2 for every classified type. No graph writes.

    review_io: when True, print human-readable step inputs/outputs to stdout
    (intended for local demo review; may include block text).
    """
    start = time.perf_counter()
    if ontology is None:
        raise RuntimeError(
            "Lane2 requires the authoritative runtime ontology"
        )
    if prompt_provider is None:
        # Prefer YAML-backed packs; fall back to mock when YAML pack build fails
        try:
            prompt_provider = YamlOntologyProvider(ontology)
            # Touch get_prompt for Process to ensure usable
            _ = prompt_provider.get_prompt("Process")
        except Exception:  # noqa: BLE001
            prompt_provider = MockCategoryExtractionProvider(ontology)

    mocked = bool(getattr(prompt_provider, "is_mocked", False))
    provider_name = type(prompt_provider).__name__
    text = _block_text_for_llm(block, masked_text)
    heading = _heading_context(block)

    _review_section(
        review_io,
        f"BLOCK START  |  {block.block_id}  |  {block.source_file}",
    )
    _review_print(
        review_io,
        f"domain={block.domain}  block_type={block.block_type}  chars={len(text)}",
        f"heading_context={heading}",
        f"prompt_provider={provider_name}  mocked={mocked}",
        f"graph_writes=False  weaviate_writes=False",
    )

    _log_step(
        "lane2.two_pass.input",
        kind="input",
        block=block,
        heading_context=heading,
        character_count=len(text),
        text_preview=_preview_text(text),
        semantic_unit_id=block.semantic_unit_id,
        source_block_ids=block.source_block_ids,
        prompt_provider=provider_name,
        mocked_ontology_or_prompt_provider=mocked,
        masked_text_provided=masked_text is not None,
        lane="lane2",
    )

    logger.info(
        "lane2.two_pass.start",
        block_id=block.block_id,
        source_file=block.source_file,
        block_index=block.block_index,
        block_type=block.block_type,
        domain=block.domain,
        heading_context=heading,
        character_count=len(text),
        semantic_unit_id=block.semantic_unit_id,
        prompt_provider=provider_name,
        mocked_ontology_or_prompt_provider=mocked,
        lane="lane2",
    )

    classification = classify_block(
        block,
        llm_callable=llm_callable,
        ontology=ontology,
        masked_text=masked_text,
        review_io=review_io,
    )

    # Summary after Pass 1 (detail already logged in classify_block start/end)
    logger.info(
        "lane2.two_pass.pass1",
        block_id=block.block_id,
        source_file=block.source_file,
        heading_context=heading,
        character_count=len(text),
        pass1_prompt_id=classification.prompt_id,
        pass1_prompt_version=classification.prompt_version,
        pass1_category=classification.category,
        pass1_categories=classification.identified_categories,
        pass1_confidence=classification.confidence,
        pass1_unmatched=classification.unmatched,
        pass1_validation_errors=classification.validation_errors,
        pass1_reasoning_summary=classification.reasoning_summary,
        llm_duration_ms=classification.llm_duration_ms,
        token_usage=classification.token_usage,
        mocked_ontology_or_prompt_provider=mocked,
        lane="lane2",
    )

    categories_to_extract = classification.identified_categories
    will_invoke_pass2 = classification.is_valid and bool(categories_to_extract)
    if classification.unmatched:
        gate_reason = "unmatched_skip_pass2"
    elif will_invoke_pass2:
        gate_reason = f"classification_valid_invoke_pass2_n={len(categories_to_extract)}"
    else:
        gate_reason = f"skip_pass2:{classification.validation_errors or ['invalid_classification']}"
    _log_step(
        "lane2.two_pass.pass1.gate",
        kind="logic",
        block=block,
        pass1_category=classification.category,
        pass1_categories=categories_to_extract,
        pass1_confidence=classification.confidence,
        is_valid=classification.is_valid,
        will_invoke_pass2=will_invoke_pass2,
        pass1_validation_errors=classification.validation_errors,
        decision=gate_reason,
        lane="lane2",
    )
    logger.info(
        "lane2.two_pass.pass1.gate",
        block_id=block.block_id,
        source_file=block.source_file,
        pass1_category=classification.category,
        pass1_categories=categories_to_extract,
        is_valid=classification.is_valid,
        will_invoke_pass2=will_invoke_pass2,
        pass1_validation_errors=classification.validation_errors,
        lane="lane2",
    )
    _review_print(
        review_io,
        "",
        f"GATE: is_valid={classification.is_valid} categories={categories_to_extract} → "
        f"{'invoke Pass 2 x' + str(len(categories_to_extract)) if will_invoke_pass2 else 'SKIP Pass 2'}",
    )

    if not will_invoke_pass2:
        total_ms = (time.perf_counter() - start) * 1000.0
        failure_reason = "unmatched" if classification.unmatched else "invalid_classification"
        _log_step(
            "lane2.two_pass.output",
            kind="output",
            block=block,
            success=False,
            failure_reason=failure_reason,
            pass1_category=classification.category,
            pass1_categories=categories_to_extract,
            pass1_confidence=classification.confidence,
            pass1_validation_errors=classification.validation_errors,
            pass2_invoked=False,
            entity_count=0,
            relationship_count=0,
            total_duration_ms=total_ms,
            lane="lane2",
        )
        logger.info(
            "lane2.two_pass.complete",
            block_id=block.block_id,
            source_file=block.source_file,
            success=False,
            failure_reason=failure_reason,
            pass1_category=classification.category,
            pass1_categories=categories_to_extract,
            pass1_validation_errors=classification.validation_errors,
            pass2_invoked=False,
            entity_count=0,
            relationship_count=0,
            extracted_entity_types=[],
            total_duration_ms=total_ms,
            mocked_ontology_or_prompt_provider=mocked,
            lane="lane2",
        )
        _review_section(review_io, f"BLOCK DONE (failed)  |  {block.block_id}  |  {total_ms:.1f} ms")
        return Lane2TwoPassResult(
            block_id=block.block_id,
            source_file=block.source_file,
            block_index=block.block_index,
            block_type=block.block_type,
            domain=block.domain,
            heading_context=heading,
            character_count=len(text),
            semantic_unit_id=block.semantic_unit_id,
            source_block_ids=block.source_block_ids,
            classification=classification,
            extraction=None,
            extractions=[],
            extraction_failures=[],
            pass2_invoked=False,
            total_duration_ms=total_ms,
            mocked_ontology_or_prompt_provider=mocked,
            success=False,
            failure_reason=failure_reason,
        )

    extractions: list[ExtractionResult] = []
    extraction_failures: list[dict[str, Any]] = []
    for cat in categories_to_extract:
        try:
            one = extract_for_category(
                block,
                cat,
                llm_callable=llm_callable,
                prompt_provider=prompt_provider,
                ontology=ontology,
                masked_text=masked_text,
                classification=classification,
                review_io=review_io,
            )
        except Exception as exc:  # noqa: BLE001 — isolate per-type failures
            logger.info(
                "lane2.two_pass.pass2.type_failed",
                block_id=block.block_id,
                extraction_type=cat,
                error=str(exc),
                lane="lane2",
            )
            extraction_failures.append(
                {"classified_node_type": cat, "error": str(exc)}
            )
            continue
        mocked = mocked or one.mocked_ontology_or_prompt_provider
        extractions.append(one)
        if one.failed:
            extraction_failures.append(
                {
                    "classified_node_type": cat,
                    "validation_errors": list(one.validation_errors),
                    "prompt_id": one.prompt_id,
                }
            )

    extraction = merge_extraction_results(classification, extractions)
    entity_types = sorted({e.get("type") for e in extraction.entities if e.get("type")})
    total_ms = (time.perf_counter() - start) * 1000.0

    logger.info(
        "lane2.two_pass.pass2",
        block_id=block.block_id,
        source_file=block.source_file,
        selected_pass2_prompt=extraction.prompt_id,
        selected_pass2_category=classification.category,
        pass2_categories=categories_to_extract,
        extraction_call_count=len(extractions),
        extraction_failure_count=len(extraction_failures),
        entity_count=len(extraction.entities),
        relationship_count=len(extraction.relationships),
        extracted_entity_types=entity_types,
        pass2_validation_errors=extraction.validation_errors,
        pass2_confidence=extraction.confidence,
        mocked_ontology_or_prompt_provider=mocked,
        lane="lane2",
    )

    # Success if at least one extraction call produced usable output, or all ran without hard fail
    hard_fail_all = bool(extractions) and all(e.failed for e in extractions)
    success = bool(extractions) and not hard_fail_all

    _log_step(
        "lane2.two_pass.output",
        kind="output",
        block=block,
        success=success,
        failure_reason=None if success else "extraction_validation_errors",
        pass1_category=classification.category,
        pass1_categories=categories_to_extract,
        pass1_confidence=classification.confidence,
        pass2_invoked=True,
        extraction_call_count=len(extractions),
        extraction_failure_count=len(extraction_failures),
        selected_pass2_prompt=extraction.prompt_id,
        entity_count=len(extraction.entities),
        relationship_count=len(extraction.relationships),
        entity_summary=_entity_summary(extraction.entities),
        relationship_summary=_rel_summary(extraction.relationships),
        pass2_validation_errors=extraction.validation_errors,
        total_duration_ms=total_ms,
        pass1_duration_ms=classification.llm_duration_ms,
        lane="lane2",
    )

    logger.info(
        "lane2.two_pass.complete",
        block_id=block.block_id,
        source_file=block.source_file,
        heading_context=heading,
        character_count=len(text),
        pass1_prompt_id=classification.prompt_id,
        pass1_prompt_version=classification.prompt_version,
        pass1_category=classification.category,
        pass1_categories=categories_to_extract,
        pass1_confidence=classification.confidence,
        pass1_validation_errors=classification.validation_errors,
        selected_pass2_prompt=extraction.prompt_id,
        selected_pass2_category=classification.category,
        extraction_call_count=len(extractions),
        entity_count=len(extraction.entities),
        relationship_count=len(extraction.relationships),
        extracted_entity_types=entity_types,
        pass2_validation_errors=extraction.validation_errors,
        total_duration_ms=total_ms,
        mocked_ontology_or_prompt_provider=mocked,
        success=success,
        lane="lane2",
    )
    _review_section(
        review_io,
        f"BLOCK DONE  |  {block.block_id}  |  success={success}  |  {total_ms:.1f} ms",
    )
    _review_print(
        review_io,
        f"Pass1: {categories_to_extract} (primary={classification.category}, conf={classification.confidence})",
        f"Pass2: calls={len(extractions)} entities={len(extraction.entities)}  "
        f"relationships={len(extraction.relationships)}  types={entity_types}",
        f"extraction_failures={extraction_failures}",
    )

    return Lane2TwoPassResult(
        block_id=block.block_id,
        source_file=block.source_file,
        block_index=block.block_index,
        block_type=block.block_type,
        domain=block.domain,
        heading_context=heading,
        character_count=len(text),
        semantic_unit_id=block.semantic_unit_id,
        source_block_ids=block.source_block_ids,
        classification=classification,
        extraction=extraction,
        extractions=extractions,
        extraction_failures=extraction_failures,
        pass2_invoked=True,
        total_duration_ms=total_ms,
        mocked_ontology_or_prompt_provider=mocked,
        success=success,
        failure_reason=None if success else "extraction_validation_errors",
    )


def canonical_block_to_review_dict(block: CanonicalBlock) -> dict[str, Any]:
    """Serialize CanonicalBlock for demo review (omit raw image bytes)."""
    data = block.model_dump(mode="json")
    if data.get("image_bytes") is not None:
        data["image_bytes"] = f"<omitted {len(block.image_bytes or b'')} bytes>"
    return data


def lane2_result_to_enriched_block(
    block: CanonicalBlock,
    result: Lane2TwoPassResult,
    ontology: dict[str, Any],
) -> EnrichedBlock | None:
    """Project two-pass result into production EnrichedBlock shape (prototype).

    Does NOT compute embeddings or run PII masking. spine_node defaults to
    domain_root (Pass 2 category packs do not request spine today).
    Returns None if classification did not yield a valid category.
    """
    category_name = result.classification.category
    if not category_name:
        return None
    try:
        block_category = BlockCategory(category_name)
    except ValueError:
        return None

    kg_node_type = _maps_to_node(ontology, block_category.value)
    if kg_node_type == "_SKIP_KG":
        kg_node_type = None

    entities = list(result.extraction.entities) if result.extraction else []
    relationships = list(result.extraction.relationships) if result.extraction else []
    text = block.text or ""

    return EnrichedBlock(
        block=block,
        block_category=block_category,
        kg_node_type=kg_node_type,
        spine_node="domain_root",
        candidate_entities=entities,
        candidate_relationships=relationships,
        embedding=[],  # not computed in lane2 two-pass prototype
        pii_spans=[],
        original_text_redacted=text,  # PII not run in this prototype
        masked_text=text,
    )


def lane2_result_to_ie_dict(
    result: Lane2TwoPassResult,
    ontology: dict[str, Any],
) -> dict[str, Any]:
    """Project a two-pass result into the IEResult-shaped dict used by IEClient.

    Invalid classification falls back to Narrative with empty extraction so the
    rest of enrich_blocks (embeddings, PII, writers) can proceed unchanged.
    """
    category = "Narrative"
    if result.classification.is_valid and result.classification.category:
        category = result.classification.category

    kg_node_type = _maps_to_node(ontology, category)
    if kg_node_type == "_SKIP_KG":
        kg_node_type = None

    entities: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    if result.extraction is not None:
        entities = list(result.extraction.entities or [])
        relationships = list(result.extraction.relationships or [])

    return {
        "block_category": category,
        "kg_node_type": kg_node_type,
        "spine_node": "domain_root",
        "entities": entities,
        "relationships": relationships,
    }


def enriched_block_to_review_dict(enriched: EnrichedBlock) -> dict[str, Any]:
    """Serialize EnrichedBlock for demo review (omit image bytes / empty embedding dump)."""
    data = enriched.model_dump(mode="json")
    block = data.get("block") or {}
    if block.get("image_bytes") is not None:
        raw = enriched.block.image_bytes
        block["image_bytes"] = f"<omitted {len(raw or b'')} bytes>"
    data["block"] = block
    emb = data.get("embedding") or []
    data["embedding"] = {
        "dims": len(emb),
        "note": "empty — embeddings not computed in lane2 two-pass prototype",
    }
    data["prototype_notes"] = {
        "pii_masking": "not applied (masked_text == raw text)",
        "spine_node": "default domain_root (not predicted in Pass 2 yet)",
        "graph_writes": False,
        "weaviate_writes": False,
    }
    return data


def make_llm_callable_from_client(llm_client: Any) -> LLMCallable:
    """Adapter to LLMClient chat helpers when a real client is available.

    Uses ChatLLMClient via LLMClient._create_chat_client when present.
    Prefer injecting a mock LLMCallable in unit tests.
    No graph / Weaviate side effects — text chat only.
    """

    def _call(system_prompt: str, user_content: str) -> tuple[str, dict[str, Any]]:
        start = time.perf_counter()
        # Prefer app ollama.text settings when available
        num_ctx = 8192
        temperature = 0.0
        try:
            from kbfabric_ingest.ingest.config import get_app_config

            ollama_cfg = get_app_config().get("ollama", {}) or {}
            text_cfg = ollama_cfg.get("text", {}) or {}
            temp_cfg = ollama_cfg.get("temperature", {}) or {}
            num_ctx = int(text_cfg.get("num_ctx", num_ctx))
            temperature = float(temp_cfg.get("ie_text", temperature))
        except Exception:  # noqa: BLE001
            pass

        if hasattr(llm_client, "_create_chat_client"):
            chat_client = llm_client._create_chat_client()
            response_text, metadata = chat_client.call_with_system_user(
                system_prompt=system_prompt,
                user_content=user_content,
                temperature=temperature,
                num_ctx=num_ctx,
                operation="lane2_two_pass",
            )
            if not isinstance(metadata, dict):
                metadata = {}
            if "duration_ms" not in metadata:
                metadata["duration_ms"] = (time.perf_counter() - start) * 1000.0
            # Surface token estimates when gateway/ollama metadata omits them
            if "token_usage" not in metadata:
                metadata["token_usage"] = {
                    "prompt_chars": len(system_prompt) + len(user_content),
                    "response_chars": len(response_text or ""),
                }
            return response_text, metadata

        # Minimal fallback: generate-style single prompt if exposed
        if hasattr(llm_client, "generate"):
            text = llm_client.generate(f"{system_prompt}\n\n{user_content}")
            return text, {"duration_ms": (time.perf_counter() - start) * 1000.0}

        raise RuntimeError("llm_client does not support chat or generate for lane2 two-pass")

    return _call

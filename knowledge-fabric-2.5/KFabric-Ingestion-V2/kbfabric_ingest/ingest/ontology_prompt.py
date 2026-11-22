"""
Ontology Prompt Builder

Builds LLM prompt sections from ontology YAML for ontology-driven IE.
No hardcoding - all context comes from the ontology configuration.
"""

from typing import Any


def _example_matches_frozen_scope(example: Any, ontology: dict) -> bool:
    if not isinstance(example, dict):
        return True
    scope = example.get("scope") or {}
    if not isinstance(scope, dict) or not scope:
        return True
    run_scope = ontology.get("frozen_run_scope") or {}
    for key in ("industry_id", "domain_id", "product_line_id", "product_line_name"):
        expected = scope.get(key)
        if expected is not None and str(expected).casefold() != str(run_scope.get(key) or "").casefold():
            return False
    return True


def build_block_categories_section(ontology: dict, *, multi: bool = False) -> str:
    """Build prompt section for block categories.

    Args:
        ontology: Loaded ontology dict.
        multi: When True, instruct the model that a block may match multiple
            categories (Lane 2 multi-entity classification). When False, keep
            the legacy single-category wording used by single-pass IE prompts.
    """
    if multi:
        header = (
            "ONTOLOGY NODE TYPES / BLOCK CATEGORIES "
            "(identify EVERY sufficiently supported type present in the block — "
            "use EXACT names as shown; a block may contain multiple types):"
        )
    else:
        header = (
            "BLOCK CATEGORIES (classify each block into ONE of these - "
            "use EXACT name as shown):"
        )
    lines = [header]

    block_categories = ontology.get("block_categories", [])
    for cat in block_categories:
        name = cat.get("name", "")
        description = cat.get("description", "")
        maps_to = cat.get("maps_to_node", cat.get("default_node_type", ""))

        if name:
            lines.append(f"  - {name}")
            if description:
                lines.append(f"    Description: {description}")
            if maps_to and maps_to != "_SKIP_KG":
                lines.append(f"    Primary node type: {maps_to}")
            elif maps_to == "_SKIP_KG":
                lines.append("    KG: semantic-only (no entity extraction needed)")
            examples = [value for value in (cat.get("examples") or []) if _example_matches_frozen_scope(value, ontology)]
            if examples:
                lines.append("    Positive examples: " + "; ".join(str(value) for value in examples[:4]))
            anti_examples = [value for value in (cat.get("anti_examples") or []) if _example_matches_frozen_scope(value, ontology)]
            if anti_examples:
                lines.append("    Negative examples: " + "; ".join(str(value) for value in anti_examples[:4]))

    return "\n".join(lines)


def build_node_types_section(ontology: dict) -> str:
    """Build prompt section for node types with examples and anti-examples."""
    lines = ["NODE TYPES for entities (use EXACT name as shown - case sensitive):"]

    node_types = ontology.get("node_types", [])
    for nt in node_types:
        if isinstance(nt, dict):
            name = nt.get("name", "")
            layer = nt.get("layer", "")
            description = nt.get("description", "")
            examples = [value for value in nt.get("examples", []) if _example_matches_frozen_scope(value, ontology)]
            anti_examples = [value for value in nt.get("anti_examples", []) if _example_matches_frozen_scope(value, ontology)]
            ie_examples = [value for value in nt.get("ie_examples", []) if _example_matches_frozen_scope(value, ontology)]
            name_pattern = nt.get("name_pattern", "")

            if not name:
                continue

            # Skip domain_structure-layer nodes in extraction prompt (pre-seeded, not LLM-extracted)
            if layer == "domain_structure":
                continue

            if description:
                lines.append(f"  - {name}: {description}")
            else:
                lines.append(f"  - {name}")
            if name_pattern:
                lines.append(f"    Naming: {name_pattern}")
            if examples:
                lines.append(f"    GOOD examples: {', '.join(str(e) for e in examples[:4])}")
            for example in ie_examples[:2]:
                snippet = str(example.get("block_text") or "").strip().replace("\n", " ")
                if snippet:
                    lines.append(f"    Industry example: {snippet[:240]}")
            if anti_examples:
                lines.append(f"    DO NOT extract: {'; '.join(str(e) for e in anti_examples[:3])}")
        elif isinstance(nt, str):
            lines.append(f"  - {nt}")

    return "\n".join(lines)


def build_relationship_types_section(ontology: dict) -> str:
    """Build prompt section for relationship types with from/to constraints."""
    lines = ["RELATIONSHIP TYPES (use EXACT name as shown - respect from/to constraints):"]

    relationship_types = ontology.get("relationship_types", [])
    for rt in relationship_types:
        name = rt.get("name", "")
        from_type = rt.get("from", "Any")
        to_type = rt.get("to", "Any")
        description = rt.get("description", "")
        example = rt.get("example", "")
        anti_example = rt.get("anti_example", "")
        examples = [value for value in (rt.get("examples") or []) if _example_matches_frozen_scope(value, ontology)]
        anti_examples = [value for value in (rt.get("anti_examples") or []) if _example_matches_frozen_scope(value, ontology)]

        if not name:
            continue

        # Format from/to — may be string or list
        from_str = ", ".join(from_type) if isinstance(from_type, list) else str(from_type)
        to_str = ", ".join(to_type) if isinstance(to_type, list) else str(to_type)

        lines.append(f"  - {name}: [{from_str}] --> [{to_str}]")
        if description:
            lines.append(f"    {description}")
        if example:
            if isinstance(example, dict):
                lines.append(f"    Example: {example.get('from', '')} → {example.get('to', '')}")
            else:
                lines.append(f"    Example: {example}")
        if example:
            lines.append(f"    Example: {example}")
        for value in examples[:3]:
            lines.append(f"    Evidence example: {value}")
        if anti_example:
            lines.append(f"    DO NOT: {anti_example}")
        for value in anti_examples[:3]:
            lines.append(f"    Do not infer: {value}")

    return "\n".join(lines)


def build_category_to_node_mapping_section(ontology: dict) -> str:
    """Build prompt section showing block_category to kg_node_type mapping."""
    lines = ["BLOCK CATEGORY to KG NODE TYPE MAPPING:"]

    # Support both old format (block_category_to_node dict) and new format (maps_to_node on categories)
    mapping = ontology.get("block_category_to_node", {})
    if mapping:
        for category, node_type in mapping.items():
            lines.append(f"  {category} -> {node_type}")
    else:
        for cat in ontology.get("block_categories", []):
            name = cat.get("name", "")
            maps_to = cat.get("maps_to_node", "")
            if name and maps_to:
                lines.append(f"  {name} -> {maps_to}")

    return "\n".join(lines)


def build_spine_hierarchy_section(ontology: dict, domain: str | None = None) -> str:
    """Build a compact spine hierarchy so the LLM can populate spine_node.
    If domain is provided, only include that domain's spine (reduces noise).
    """
    lines = [
        "SPINE HIERARCHY — use these EXACT names (case-sensitive) for the spine_node field:",
        "CRITICAL: You MUST include spine_node in every block's JSON. Pick the SubFunction or Function that best matches the block's content.",
    ]
    for d in ontology.get("domains", []):
        if not isinstance(d, dict):
            continue
        domain_name = d.get("name", "unknown")
        if domain and domain_name != domain:
            continue
        spine = d.get("spine", [])
        if not spine:
            continue
        domain_display_name = d.get("display_name") or domain_name
        lines.append(f"  Domain: {domain_display_name} (ID: {domain_name})")
        for pl in spine:
            if not isinstance(pl, dict):
                continue
            pl_name = pl.get("ProductLine", "")
            if not pl_name:
                continue
            lines.append(f"    ProductLine: {pl_name}")
            for fn in pl.get("functions", []):
                if not isinstance(fn, dict):
                    continue
                fn_name = fn.get("Function", "")
                if not fn_name:
                    continue
                subs = fn.get("subfunctions", [])
                if subs:
                    lines.append(f"      Function: {fn_name}  →  {', '.join(subs)}")
                else:
                    lines.append(f"      Function: {fn_name}")
    return "\n".join(lines)


def build_full_ontology_prompt(ontology: dict, domain: str | None = None) -> str:
    """
    Build complete ontology context section for IE prompt.
    Includes block categories, node types, relationship types, mappings,
    and the spine hierarchy so the LLM can assign spine_node values.
    If domain is provided, only that domain's spine is shown (clearer for LLM).
    """
    sections = []

    sections.append("=" * 60)
    sections.append("ONTOLOGY CONTEXT - READ CAREFULLY")
    sections.append("=" * 60)
    sections.append("")
    frozen_scope = ontology.get("frozen_run_scope") or {}
    if frozen_scope:
        sections.append("FROZEN INGESTION SCOPE (authoritative; do not infer or change):")
        for key in ("industry_id", "customer_id", "tenant_id", "domain_id", "product_line_id", "product_line_name", "customer_ontology_id", "customer_ontology_version"):
            if frozen_scope.get(key) is not None:
                sections.append(f"- {key}: {frozen_scope[key]}")
        sections.append("")
    sections.append("CRITICAL INSTRUCTIONS:")
    sections.append("1. Use EXACT names as shown (case-sensitive): 'Incident' NOT 'incident', 'Process' NOT 'process'")
    sections.append("2. Classify blocks based on DESCRIPTION and EXAMPLES, not just keywords")
    sections.append("3. For entities, use node types from NODE TYPES list only")
    sections.append("4. For relationships, use types from RELATIONSHIP TYPES list only")
    sections.append("5. Respect from/to constraints when creating relationships")
    sections.append("6. Extract only source-evidenced relationships matching an exact allowed endpoint triple; an empty relationship list is valid")
    sections.append("7. spine_node: Pick SubFunction or Function from SPINE HIERARCHY below. Use 'domain_root' only if none fits.")
    sections.append("")

    spine_section = build_spine_hierarchy_section(ontology, domain)
    if spine_section.count("\n") > 1:
        sections.append(spine_section)
        sections.append("")

    sections.append(build_block_categories_section(ontology))
    sections.append("")
    sections.append(build_node_types_section(ontology))
    sections.append("")
    sections.append(build_relationship_types_section(ontology))
    sections.append("")
    sections.append(build_category_to_node_mapping_section(ontology))
    sections.append("")
    sections.append("=" * 60)

    return "\n".join(sections)


def get_ie_examples_from_ontology(ontology: dict) -> list[dict]:
    """
    Extract IE examples from node type definitions in the ontology YAML.
    These replace the old kbfabric.yaml ie_examples.
    Returns a list of example dicts compatible with the prompt format.
    """
    examples = []
    for nt in ontology.get("node_types", []):
        if not isinstance(nt, dict):
            continue
        for ie_ex in nt.get("ie_examples", []):
            if not _example_matches_frozen_scope(ie_ex, ontology):
                continue
            examples.append({
                "block_text": ie_ex.get("block_text", ""),
                "block_category": ie_ex.get("block_category", ""),
                "kg_node_type": nt.get("name", ""),
                "spine_node": ie_ex.get("spine_node", "domain_root"),
                "entities": ie_ex.get("entities", []),
                "relationships": ie_ex.get("relationships", []),
            })
    return examples


def format_ie_examples_for_prompt(examples: list[dict]) -> str:
    """
    Format IE examples into a clear few-shot section for the LLM prompt.
    Groups examples by kg_node_type so the LLM sees per-category demonstrations.
    """
    import json as _json

    if not examples:
        return ""

    grouped: dict[str, list[dict]] = {}
    for ex in examples:
        key = ex.get("kg_node_type", "Unknown")
        grouped.setdefault(key, []).append(ex)

    lines = [
        "=" * 60,
        "FEW-SHOT EXAMPLES — study these carefully before responding",
        "=" * 60,
    ]

    for node_type, exs in grouped.items():
        lines.append(f"\n--- Node type: {node_type} ---")
        for i, ex in enumerate(exs, 1):
            lines.append(f"\nExample {node_type}-{i}:")
            lines.append(f"  INPUT TEXT: \"{ex.get('block_text', '')}\"")
            lines.append(f"  EXPECTED OUTPUT:")
            output = {
                "block_category": ex.get("block_category", ""),
                "kg_node_type": ex.get("kg_node_type", ""),
                "spine_node": ex.get("spine_node", "domain_root"),
                "entities": ex.get("entities", []),
                "relationships": ex.get("relationships", []),
            }
            for output_line in _json.dumps(output, indent=4).split("\n"):
                lines.append(f"    {output_line}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


CLASSIFICATION_DECISION_TREE = """
CLASSIFICATION PRIORITY — use the supplied ontology category definitions, examples, and exclusions.
Resolve the active industry, domain, and product line only from the frozen run context.
1. Explicit operational outage/event -> Incident.
2. Reported malfunction or unintended product behavior -> Defect.
3. Explicitly named external legal, regulatory, or standards instrument -> Regulation.
4. Concrete verification, authorization, restriction, or reconciliation mechanism -> Control.
5. Explicit system/business obligation or constraint -> Requirement.
6. Conditional rule with both condition and resulting decision/action -> Rule.
7. Explicit role goal or requested capability -> UserStory.
8. Testable condition of satisfaction -> AcceptanceCriteria.
9. Individual test procedure with verification intent -> TestCase.
10. Machine-readable interface or technical contract -> TechnicalSpec.
11. Scheduled job, configuration, deployment, or system event -> TechnicalOps.
12. Identified data capture form or screen -> FormCapture.
13. Visual content -> Diagram.
14. Glossary, definition, or reference material -> Reference.
15. Named executable operational procedure supported by evidence -> Process.
16. General prose -> Narrative, only when no more specific category fits.
Use only categories defined by the runtime ontology. Do not apply this order as a substitute for
explicit category definitions or exclusions, and do not invent a category absent from the ontology.
""".strip()


def get_allowed_category_names(ontology: dict) -> list[str]:
    """Get list of allowed block category names."""
    return [cat.get("name", "") for cat in ontology.get("block_categories", []) if cat.get("name")]


def get_allowed_node_types(ontology: dict) -> list[str]:
    """Get list of allowed node type names."""
    node_types = []
    for nt in ontology.get("node_types", []):
        if isinstance(nt, dict):
            name = nt.get("name", "")
            if name:
                node_types.append(name)
        elif isinstance(nt, str):
            node_types.append(nt)
    return node_types


def get_allowed_relationship_types(ontology: dict) -> list[str]:
    """Get list of allowed relationship type names."""
    return [rt.get("name", "") for rt in ontology.get("relationship_types", []) if rt.get("name")]


def normalize_to_ontology(value: str, allowed_values: list[str]) -> str:
    """
    Normalize a value to match ontology casing.
    Returns the correctly cased value if found, otherwise original value.
    """
    if not value:
        return value
    lookup = {v.lower(): v for v in allowed_values}
    return lookup.get(value.lower(), value)

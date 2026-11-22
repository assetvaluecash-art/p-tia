"""Display formatting for graph nodes in retrieval context.

Canonical identity (Neo4j ``name`` / IE ``value``) stays unchanged.
Retrieval/LLM-facing strings may append an instance or ontology description.
"""

from __future__ import annotations

from typing import Any, Mapping


def format_node_for_retrieval(
    canonical_name: str | None,
    *,
    instance_description: str | None = None,
    ontology_type_description: str | None = None,
) -> str:
    """Return ``ID : description`` when a description is available, else the ID.

    Preference order:
    1. Non-empty instance description (from Neo4j ``description``)
    2. Ontology node-type description (fallback)
    3. Canonical name alone
    """
    name = (canonical_name or "").strip() or "Unknown"
    instance = (instance_description or "").strip()
    if instance:
        # Avoid "FR-04 : FR-04 : ..." if a caller already joined them
        if instance.lower().startswith(name.lower() + " :") or instance.lower().startswith(name.lower() + ":"):
            return instance
        return f"{name} : {instance}"

    ontology_desc = (ontology_type_description or "").strip()
    if ontology_desc:
        return f"{name} : {ontology_desc}"

    return name


def ontology_type_descriptions(ontology: Mapping[str, Any] | None) -> dict[str, str]:
    """Map node type name -> ontology description string."""
    out: dict[str, str] = {}
    if not ontology:
        return out
    for nt in ontology.get("node_types") or []:
        if not isinstance(nt, dict):
            continue
        name = (nt.get("name") or "").strip()
        desc = (nt.get("description") or "").strip()
        if name and desc:
            out[name] = desc
    return out

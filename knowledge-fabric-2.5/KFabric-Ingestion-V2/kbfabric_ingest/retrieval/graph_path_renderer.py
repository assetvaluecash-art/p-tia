"""Graph path renderer for LLM-friendly sentence-like representations.

Transforms raw Neo4j graph paths into natural language sentences that are:
- Readable by SMEs and QE teams
- Highly effective for LLM reasoning
- Aligned with the domain ontology

The relationship verbs are loaded from enterprise_banking_ontology.yaml (SINGLE SOURCE OF TRUTH).

Example transformation:
    Input:  GraphPath(from_entity={name:"ESG Drift Detection", type:"Process"}, 
                     relationship_type="NEXT_STEP",
                     to_entity={name:"ESG Constraint Evaluation", type:"Process"})
    
    Output: '[wealth_portfolio] Process "ESG Drift Detection" is followed by process "ESG Constraint Evaluation".'
"""

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from kbfabric_ingest.retrieval.models import GraphPath, RerankedPath


# Scaffolding relationships to filter out - these are KG plumbing, not domain semantics
SCAFFOLDING_RELATIONSHIPS = frozenset({
    'NODE_IN_DOMAIN',
    'BLOCK_BELONGS_TO_DOCUMENT', 
    'DOCUMENT_IN_DOMAIN',
    'BELONGS_TO',
    'HAS_BLOCK',
    'IN_DOCUMENT',
    'CONTAINS_BLOCK'
})


def _get_ontology_path() -> Path | None:
    """
    Get ontology file path from THIS PROJECT's config folder only.
    No root folder references - each project is a standalone deployable unit.
    """
    # Check this module's project config folder
    # Path: kbfabric_ingest/retrieval/ -> kbfabric_ingest/ -> config/
    project_config = Path(__file__).parent.parent.parent / "config" / "enterprise_banking_ontology.yaml"
    if project_config.exists():
        return project_config
    
    return None


def _load_ontology() -> dict:
    """Load the full ontology from enterprise_banking_ontology.yaml."""
    ontology_path = _get_ontology_path()
    if not ontology_path:
        import logging
        logging.warning("Could not find enterprise_banking_ontology.yaml in project config folder")
        return {}
    
    try:
        with open(ontology_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        import logging
        logging.warning(f"Failed to load ontology: {e}")
        return {}


def _load_verbs_from_ontology() -> dict[str, str]:
    """
    Load relationship verbs from enterprise_banking_ontology.yaml (SINGLE SOURCE OF TRUTH).
    
    Sources (in priority order):
    1. path_format.rel_type_to_natural (primary - designed for LLM context)
    2. relationship_types[].verb (secondary - from ontology definition)
    
    NO HARDCODED FALLBACKS - all verbs must come from ontology.
    """
    ontology = _load_ontology()
    verbs = {}
    
    # Priority 1: path_format.rel_type_to_natural (designed for LLM-friendly output)
    path_format = ontology.get("path_format", {})
    rel_to_natural = path_format.get("rel_type_to_natural", {})
    verbs.update(rel_to_natural)
    
    # Priority 2: relationship_types[].verb (from ontology definition)
    for rel in ontology.get("relationship_types", []):
        name = rel.get("name")
        verb = rel.get("verb")
        if name and verb and name not in verbs:
            verbs[name] = verb
    
    # Default for unknown relationships
    verbs["_DEFAULT"] = "relates to"
    
    return verbs


def _load_path_format_config() -> dict:
    """
    Load path format configuration from ontology.
    
    Returns:
        dict with keys: style, include_evidence, include_provenance, template
    """
    ontology = _load_ontology()
    path_format = ontology.get("path_format", {})
    
    return {
        "style": path_format.get("style", "natural_with_metadata"),
        "include_evidence": path_format.get("include_evidence", True),
        "include_provenance": path_format.get("include_provenance", True),
        "template": path_format.get("template", 
            "RELATIONSHIP: {from_name} {rel_type_natural} {to_name}\n"
            "  TYPE: {from_type} -> {to_type} ({rel_type})\n"
            '  EVIDENCE: "{source_evidence}"\n'
            "  SOURCE: {source_file} [Page: {source_page}, Block: {source_block_id}]"
        ),
    }


# Load at module import time (cached until service restart)
RELATIONSHIP_VERBS = _load_verbs_from_ontology()
PATH_FORMAT_CONFIG = _load_path_format_config()


def is_scaffolding_relationship(rel_type: str) -> bool:
    """Check if a relationship type is scaffolding (KG plumbing, not domain semantics)."""
    return rel_type in SCAFFOLDING_RELATIONSHIPS


def get_relationship_verb(rel_type: str) -> str:
    """Get the natural language verb for a relationship type."""
    return RELATIONSHIP_VERBS.get(rel_type, RELATIONSHIP_VERBS["_DEFAULT"])


def format_path_from_ontology(
    from_name: str,
    from_type: str,
    rel_type: str,
    to_name: str,
    to_type: str,
    domain: str | None = None,
    source_evidence: str | None = None,
    source_file: str | None = None,
    source_page: int | None = None,
    source_block_id: str | None = None,
    hub_name: str | None = None,
    hub_type: str | None = None,
    hub_pagerank: float | None = None,
    hub_degree: float | None = None,
    hub_rel_type: str | None = None,
    hub_rel_dir: str | None = None,
) -> str:
    """
    Format path using template from enterprise_banking_ontology.yaml (SINGLE SOURCE OF TRUTH).
    
    The template and verb mappings come entirely from the ontology's path_format section.
    No hardcoding - change ontology to change output format.
    """
    # Get natural verb from ontology
    rel_type_natural = get_relationship_verb(rel_type)
    hub_rel_type_natural = get_relationship_verb(hub_rel_type) if hub_rel_type else RELATIONSHIP_VERBS["_DEFAULT"]
    
    # Get template from ontology config
    template = PATH_FORMAT_CONFIG.get("template", "")
    
    # Build chain strings (symbolic + natural), with safe fallbacks.
    dom = (domain or "unknown_domain").strip() or "unknown_domain"
    use_hub = bool(hub_name and hub_type and hub_rel_type)
    hub_name_eff = hub_name or from_name or "Unknown"
    hub_type_eff = hub_type or from_type or "Entity"
    hub_pr_eff = hub_pagerank if hub_pagerank is not None else 0.0
    hub_deg_eff = hub_degree if hub_degree is not None else 0.0

    if use_hub:
        hub_arrow = "->" if hub_rel_dir == "hub_to_from" else "<-" if hub_rel_dir == "from_to_hub" else "-"
        chain_symbolic = (
            f'(DomainRoot:{dom})<-[:NODE_IN_DOMAIN]-'
            f'(Hub:{hub_type_eff}:"{hub_name_eff}")'
            f'-[:{hub_rel_type}]{hub_arrow}'
            f'(From:{from_type}:"{from_name}")'
            f'-[:{rel_type}]->'
            f'(To:{to_type}:"{to_name}")'
        )
        if hub_rel_dir == "hub_to_from":
            hub_to_from_natural = f'{hub_type_eff} "{hub_name_eff}" {hub_rel_type_natural} {from_type} "{from_name}"'
        elif hub_rel_dir == "from_to_hub":
            hub_to_from_natural = (
                f'{hub_type_eff} "{hub_name_eff}" is related to {from_type} "{from_name}" '
                f'(edge: {from_type} "{from_name}" {hub_rel_type_natural} {hub_type_eff.lower()} "{hub_name_eff}")'
            )
        else:
            hub_to_from_natural = f'{hub_type_eff} "{hub_name_eff}" is connected to {from_type} "{from_name}" via {hub_rel_type}'
        chain_natural = (
            f'In domain {dom}, hub {hub_type_eff} "{hub_name_eff}" (pagerank={hub_pr_eff:.3f}, degree={int(hub_deg_eff)}) '
            f'{hub_to_from_natural}, which {rel_type_natural} {to_type.lower()} "{to_name}".'
        )
    else:
        chain_symbolic = (
            f'(DomainRoot:{dom})<-[:NODE_IN_DOMAIN]-'
            f'(From:{from_type}:"{from_name}")'
            f'-[:{rel_type}]->'
            f'(To:{to_type}:"{to_name}")'
        )
        chain_natural = (
            f'In domain {dom}, {from_type} "{from_name}" {rel_type_natural} {to_type.lower()} "{to_name}".'
        )

    # Prepare values (with safe defaults for missing data)
    values = {
        "domain": dom,
        "from_name": from_name or "Unknown",
        "from_type": from_type or "Entity",
        "rel_type": rel_type or "RELATES_TO",
        "rel_type_natural": rel_type_natural,
        "to_name": to_name or "Unknown",
        "to_type": to_type or "Entity",
        "source_evidence": (source_evidence[:200] + "..." if source_evidence and len(source_evidence) > 200 else source_evidence) or "N/A",
        "source_file": source_file or "N/A",
        "source_page": source_page if source_page is not None else "N/A",
        "source_block_id": source_block_id or "N/A",
        "hub_name": hub_name_eff,
        "hub_type": hub_type_eff,
        "hub_pagerank": round(float(hub_pr_eff or 0.0), 4),
        "hub_degree": int(hub_deg_eff or 0),
        "hub_rel_type": hub_rel_type or "N/A",
        "hub_rel_type_natural": hub_rel_type_natural,
        "chain_symbolic": chain_symbolic,
        "chain_natural": chain_natural,
    }
    
    try:
        return template.format(**values).strip()
    except KeyError as e:
        import logging
        logging.warning(f"Path format template has unknown placeholder: {e}")
        return f"RELATIONSHIP: {from_name} {rel_type_natural} {to_name}"


class GraphPathRenderer:
    """Renders graph paths as natural language sentences for LLM prompts."""
    
    def __init__(self):
        """Initialize the renderer."""
        pass
    
    def render_path(self, path: "GraphPath | RerankedPath", domain: str) -> str:
        """
        Convert a GraphPath to a sentence-like format.
        
        Args:
            path: GraphPath or RerankedPath object
            domain: Domain name (e.g., "wealth_portfolio")
            
        Returns:
            Sentence-like string, e.g.:
            '[wealth_portfolio] Process "ESG Drift Detection" is followed by process "ESG Constraint Evaluation".'
        """
        # Get relationship verb
        rel_type = getattr(path, 'relationship_type', None)
        if not rel_type:
            return self._render_from_path_text(path, domain)
        
        verb = get_relationship_verb(rel_type)
        
        # Get entity details
        from_entity = getattr(path, 'from_entity', {})
        to_entity = getattr(path, 'to_entity', {})
        
        from_type = from_entity.get("type", "Entity")
        to_type = to_entity.get("type", "Entity")
        from_name = from_entity.get("name", "Unknown")
        to_name = to_entity.get("name", "Unknown")
        
        # Format domain for display
        domain_display = domain.replace('_', ' ')
        
        # Build sentence
        return f'[{domain_display}] {from_type} "{from_name}" {verb} {to_type.lower()} "{to_name}".'
    
    def _render_from_path_text(self, path: "GraphPath | RerankedPath", domain: str) -> str:
        """Fallback renderer when path_text is the primary source."""
        path_text = getattr(path, 'path_text', str(path))
        domain_display = domain.replace('_', ' ')
        return f'[{domain_display}] {path_text}'
    
    def render_paths_for_prompt(
        self, 
        paths: "list[GraphPath | RerankedPath]", 
        domain: str,
        max_paths: int = 15
    ) -> str:
        """
        Render multiple paths as a numbered list for LLM prompt.
        
        Args:
            paths: List of GraphPath or RerankedPath objects
            domain: Domain name
            max_paths: Maximum number of paths to include
            
        Returns:
            Formatted string with header and numbered paths
        """
        if not paths:
            return ""
        
        # Filter out scaffolding relationships
        filtered_paths = [
            p for p in paths 
            if not is_scaffolding_relationship(getattr(p, 'relationship_type', ''))
        ]
        
        if not filtered_paths:
            return ""
        
        # Limit to max_paths
        paths_to_render = filtered_paths[:max_paths]
        
        # Format domain for display
        domain_display = domain.replace('_', ' ')
        
        # Build output
        lines = [f"Structural knowledge from the {domain_display} knowledge graph:"]
        for i, path in enumerate(paths_to_render, 1):
            rendered = self.render_path(path, domain)
            lines.append(f"{i}. {rendered}")
        
        return "\n".join(lines)
    
    def render_path_for_report(self, path: "GraphPath | RerankedPath", domain: str) -> str:
        """
        Render a path for HTML report display.
        Similar to render_path but may include additional formatting.
        
        Args:
            path: GraphPath or RerankedPath object
            domain: Domain name
            
        Returns:
            HTML-safe sentence-like string
        """
        return self.render_path(path, domain)


# Singleton instance for convenience
_renderer = None

def get_renderer() -> GraphPathRenderer:
    """Get the singleton GraphPathRenderer instance."""
    global _renderer
    if _renderer is None:
        _renderer = GraphPathRenderer()
    return _renderer


def render_path(path: "GraphPath | RerankedPath", domain: str) -> str:
    """Convenience function to render a single path."""
    return get_renderer().render_path(path, domain)


def render_paths_for_prompt(
    paths: "list[GraphPath | RerankedPath]", 
    domain: str,
    max_paths: int = 15
) -> str:
    """Convenience function to render paths for LLM prompt."""
    return get_renderer().render_paths_for_prompt(paths, domain, max_paths)



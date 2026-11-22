"""Config loader for KnowledgeFabric Ingestion.

Application settings come from repo-root ``env`` via ``env_config.build_app_config``.
Ontology YAML files remain on disk.
"""

from pathlib import Path
from typing import Any, Dict
import yaml

from kbfabric_ingest._common.env_config import build_app_config

# Base directory is the package root (kbfabric-ingest/)
BASE_DIR = Path(__file__).resolve().parents[2]
ONTOLOGY_PATH = BASE_DIR / "config" / "enterprise_banking_ontology.yaml"

_config_cache: Dict[str, Any] | None = None


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load YAML file."""
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_app_config() -> Dict[str, Any]:
    """Return ingestion config built from environment variables (cached)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = build_app_config()
    return _config_cache


def get_ontology() -> Dict[str, Any]:
    """Return ontology skeleton from enterprise_banking_ontology.yaml."""
    if ONTOLOGY_PATH.exists():
        return load_yaml(ONTOLOGY_PATH)
    return {}


def get_domain_to_class() -> Dict[str, str]:
    """
    Get domain to Weaviate class name mapping.

    DEPRECATED: Use kbfabric_ingest.ingest.config.get_domain_to_class() instead.
    This function is kept for backward compatibility and redirects to canonical implementation.

    Returns:
        Dict mapping domain names to store names
    """
    from kbfabric_ingest.ingest.config import get_domain_to_class as canonical_get_domain_to_class
    return canonical_get_domain_to_class()


def get_domains_list() -> list:
    """
    Get list of available domain names from ontology.

    Returns:
        List of domain name strings
    """
    ontology = get_ontology()
    domain_list = ontology.get("domains", [])
    return [d.get("name") for d in domain_list if d.get("name")]


def get_domains_full() -> list:
    """
    Get list of available domains with full metadata from ontology.

    Returns:
        List of domain objects with name, display_name, and description
    """
    ontology = get_ontology()
    domain_list = ontology.get("domains", [])

    domains = []
    for domain in domain_list:
        if domain.get("name"):
            domains.append({
                "name": domain["name"],
                "display_name": domain.get("display_name", domain["name"]),
                "description": domain.get("description", "")
            })

    return domains


def get_ie_examples(domain: str = None) -> list:
    """
    Get IE few-shot examples from config.

    Args:
        domain: Optional domain name to get domain-specific examples

    Returns:
        List of example dictionaries for few-shot prompting
    """
    config = get_app_config()

    if domain:
        domains = config.get("domains", {})
        domain_config = domains.get(domain, {})
        domain_examples = domain_config.get("ie_examples", [])
        if domain_examples:
            return domain_examples

    return config.get("ie_default_examples", [])

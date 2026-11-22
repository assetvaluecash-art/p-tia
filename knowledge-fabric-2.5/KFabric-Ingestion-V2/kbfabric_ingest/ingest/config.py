"""Config loader for KBFabricKB ingestion.

Application settings are loaded exclusively from repo-root ``env`` (see
``kbfabric_ingest._common.env_config``). Ontology YAML files remain on disk.
"""

from pathlib import Path
from typing import Any, Dict, Optional
import yaml

from kbfabric_ingest._common.env_config import build_app_config

BASE_DIR = Path(__file__).resolve().parents[2]
ONTOLOGY_PATH = BASE_DIR / "config" / "enterprise_banking_ontology.yaml"
ONTOLOGY_VISION_PATH = BASE_DIR / "config" / "ontology_vision.yaml"

# Module-level caches to avoid repeated file / env reads
_ontology_cache: Optional[Dict[str, Any]] = None
_ontology_vision_cache: Optional[Dict[str, Any]] = None
_config_cache: Optional[Dict[str, Any]] = None


def load_yaml(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_app_config() -> Dict[str, Any]:
    """Return application config built from environment variables (cached)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = build_app_config()
    return _config_cache


def get_ontology() -> Dict[str, Any]:
    """Return ontology skeleton from enterprise_banking_ontology.yaml (cached)."""
    global _ontology_cache
    if _ontology_cache is None:
        _ontology_cache = load_yaml(ONTOLOGY_PATH)
    return _ontology_cache


def get_vision_ontology() -> Dict[str, Any]:
    """Return minimal vision ontology from ontology_vision.yaml (cached)."""
    global _ontology_vision_cache
    if _ontology_vision_cache is None:
        _ontology_vision_cache = load_yaml(ONTOLOGY_VISION_PATH)
    return _ontology_vision_cache


def clear_caches():
    """Clear all config caches. Useful for testing or config reloads."""
    global _ontology_cache, _ontology_vision_cache, _config_cache
    _ontology_cache = None
    _ontology_vision_cache = None
    _config_cache = None


def _store_name_from_domain_name(name: str) -> str:
    """Derive Weaviate class / store name from ontology domain name (e.g. corporate_commercial -> Corporate_commercial)."""
    if not name:
        return ""
    return name[0].upper() + name[1:]


def get_domain_to_class() -> Dict[str, str]:
    """
    Get domain to Weaviate class name mapping from ontology (single source of truth).

    Store name is derived from each domain's name: first character uppercased
    (e.g. corporate_commercial -> Corporate_commercial). Optional overrides
    can be provided via domains.<name>.store_name in the env-built config.

    Returns:
        Dict mapping domain names to store names (used for Weaviate class AND Neo4j domain_root)
        Example: {"consumer_retail": "Consumer_retail", ...}

    Raises:
        ValueError: If ontology has no domains
    """
    ontology = get_ontology()
    domain_list = ontology.get("domains", [])
    if not domain_list:
        raise ValueError(
            "No domains defined in ontology. Check config/enterprise_banking_ontology.yaml 'domains' section."
        )
    config = get_app_config()
    config_domains = config.get("domains", {}) or {}
    domain_to_store: Dict[str, str] = {}
    for d in domain_list:
        name = d.get("name") if isinstance(d, dict) else None
        if not name or not isinstance(name, str):
            continue
        store_name = (config_domains.get(name) or {}).get("store_name") if isinstance(config_domains.get(name), dict) else None
        if not store_name:
            store_name = _store_name_from_domain_name(name)
        domain_to_store[name] = store_name
    if not domain_to_store:
        raise ValueError(
            "No valid domain names in ontology. Ensure ontology has 'domains' list with 'name' for each."
        )
    return domain_to_store


def get_store_name_for_domain(domain: str) -> str:
    """
    Get store name (Weaviate class / Neo4j domain_root) for a domain.

    NO FALLBACKS - fails fast if domain not found.

    Args:
        domain: Domain name (e.g., "consumer_retail")

    Returns:
        Store name (e.g., "Consumer_retail")

    Raises:
        ValueError: If domain not found in config
    """
    domain_to_class = get_domain_to_class()

    if domain not in domain_to_class:
        valid_domains = sorted(domain_to_class.keys())
        raise ValueError(
            f"Domain '{domain}' not found in config. "
            f"Valid domains: {valid_domains}"
        )

    return domain_to_class[domain]


def get_ie_examples(domain: str = None) -> list:
    """
    Get IE few-shot examples from config.

    Domain-specific examples are used if available, otherwise falls back
    to default examples. This enables easy domain onboarding via config.

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

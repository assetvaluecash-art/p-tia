from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class IndustryConfigError(ValueError):
    """Raised when an industry configuration is missing or invalid."""


@dataclass(frozen=True)
class IndustryConfig:
    """Loaded canonical configuration for one industry.

    The three YAML documents remain separate because they have distinct
    architectural ownership: business ontology, graph model, and extraction
    policy.
    """

    industry_id: str
    ontology: dict[str, Any]
    model: dict[str, Any]
    extraction_policy: dict[str, Any]


class IndustryConfigLoader:
    """Load canonical industry configuration from config/industries."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._uses_repository_registry = base_dir is None
        config_root = Path(__file__).resolve().parents[3] / "config"
        self.base_dir = base_dir or (config_root / "industries")
        self.canonical_model_path = config_root / "canonical" / "model.yaml"

    def load(self, industry_id: str) -> IndustryConfig:
        normalized = industry_id.strip()
        if not normalized:
            raise IndustryConfigError("industry_id must not be blank")

        # Persisted ontology IDs may differ from industry config directory names.
        config_id = {"enterprise_banking": "banking"}.get(normalized, normalized)
        industry_dir = self.base_dir / config_id
        if not industry_dir.is_dir():
            raise IndustryConfigError(
                f"Industry configuration directory not found: {industry_dir}"
            )

        if self._uses_repository_registry:
            ontology_path = industry_dir / "ontology.yaml"
            model_path = self.canonical_model_path
        else:
            # Preserve isolated loader fixtures that provide a complete local set.
            ontology_path = industry_dir / "ontology.yaml"
            model_path = industry_dir / "model.yaml"

        ontology = self._load_yaml(ontology_path)
        model = self._load_yaml(model_path)
        extraction_policy = self._load_yaml(industry_dir / "extraction_policy.yaml")
        policy_industry_id = str(extraction_policy.get("industry_id") or config_id)
        if policy_industry_id != config_id:
            raise IndustryConfigError(
                f"Extraction policy industry_id mismatch: expected {config_id}, "
                f"got {policy_industry_id}"
            )

        return IndustryConfig(
            industry_id=config_id,
            ontology=ontology,
            model=model,
            extraction_policy=extraction_policy,
        )

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise IndustryConfigError(
                f"Required industry configuration file not found: {path}"
            )

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)

        if not isinstance(data, dict):
            raise IndustryConfigError(
                f"Industry configuration must be a YAML mapping: {path}"
            )

        return data
